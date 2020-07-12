import numpy as np
import tensorflow as tf
import tensorflow_probability as tfp
from flow_models import utils
from flow_models import flow_builder
from pipeline import data_loader
import argparse
import time
import os
import sys
from train_utils import *
tfd = tfp.distributions
tfb = tfp.bijectors
tfk = tf.keras


def train(mirrored_strategy, args, flow, optimizer, ds_dist, ds_val_dist,
          manager, manager_issues, train_summary_writer, test_summary_writer):
    # Custom Training Step
    # Adding the tf.function makes it about 10 times faster!!!

    with mirrored_strategy.scope():
        def compute_train_loss(X):
            per_example_loss = -flow.log_prob(X)
            return tf.nn.compute_average_loss(per_example_loss, global_batch_size=args.batch_size)

        def compute_test_loss(X):
            per_example_loss = -flow.log_prob(X)
            return tf.nn.compute_average_loss(per_example_loss, global_batch_size=args.test_batch_size)

    def train_step(inputs):
        with tf.GradientTape() as tape:
            tape.watch(flow.trainable_variables)
            loss = compute_train_loss(inputs)
        gradients = tape.gradient(loss, flow.trainable_variables)
        optimizer.apply_gradients(
            list(zip(gradients, flow.trainable_variables)))
        return loss

    def test_step(inputs):
        loss = compute_test_loss(inputs)
        return loss

    @tf.function
    def distributed_train_step(dataset_inputs):
        per_replica_losses = mirrored_strategy.run(
            train_step, args=(dataset_inputs,))
        return mirrored_strategy.reduce(tf.distribute.ReduceOp.SUM, per_replica_losses, axis=None)

    @tf.function
    def distributed_test_step(dataset_inputs):
        per_replica_losses = mirrored_strategy.run(
            test_step, args=(dataset_inputs,))
        return mirrored_strategy.reduce(tf.distribute.ReduceOp.SUM, per_replica_losses, axis=None)

    # Display first generated samples
    with mirrored_strategy.scope():
        samples = flow.sample(32)
    if args.img_type == "image":
        samples += 0.5
    samples = samples.numpy().reshape([32] + args.data_shape)
    try:
        figure = image_grid(samples, args.data_shape, args.img_type,
                            sampling_rate=args.sampling_rate, fmin=args.fmin, fmax=args.fmax)
        with train_summary_writer.as_default():
            tf.summary.image("32 generated samples", plot_to_image(figure), max_outputs=50, step=0)
    except IndexError:
        with train_summary_writer.as_default():
            tf.summary.text(name="display error",
                            data="Impossible to display spectrograms because of NaN values", step=0)

    N_EPOCHS = args.n_epochs
    batch_size = args.batch_size
    t0 = time.time()
    loss_history = []
    count_step = optimizer.iterations.numpy()
    min_val_loss = 0.
    prev_history_loss_avg = None
    loss_per_epoch = 10  # number of losses per epoch to save
    is_nan_loss = False
    test_loss = tfk.metrics.Mean(name='test_loss')
    history_loss_avg = tf.keras.metrics.Mean(name="tensorboard_loss")
    epoch_loss_avg = tf.keras.metrics.Mean(name="epoch_loss")
    n_train = args.n_train
    print("Start Training on {} epochs".format(N_EPOCHS))
    # Custom Training Loop
    for epoch in range(1, N_EPOCHS + 1):
        epoch_loss_avg.reset_states()

        if is_nan_loss:
            break

        for batch in ds_dist:
            loss = distributed_train_step(batch)
            history_loss_avg.update_state(loss)
            epoch_loss_avg.update_state(loss)
            count_step += 1

            # every loss_per_epoch train step
            if count_step % (n_train // (batch_size * loss_per_epoch)) == 0:
                # check nan loss
                if (tf.math.is_nan(loss)) or (tf.math.is_inf(loss)):
                    print('Nan or Inf Loss: {}'.format(loss))
                    is_nan_loss = True
                    break

                # Save history and monitor it on tensorboard
                curr_loss_history = history_loss_avg.result()
                loss_history.append(curr_loss_history)
                with train_summary_writer.as_default():
                    step_int = int(loss_per_epoch * count_step * batch_size / n_train)
                    tf.summary.scalar(
                        'loss', curr_loss_history, step=step_int)

                if manager_issues is not None:
                    # look for huge jump in the loss
                    if prev_history_loss_avg is None:
                        prev_history_loss_avg = curr_loss_history
                    elif curr_loss_history - prev_history_loss_avg > 10**6:
                        print("Huge gap in the loss")
                        save_path = manager_issues.save()
                        print("Model weights saved at {}".format(save_path))
                        with train_summary_writer.as_default():
                            tf.summary.text(name='Loss Jump',
                                            data=tf.constant(
                                                "Huge jump in the loss. Model weights saved at {}".format(save_path)),
                                            step=step_int)

                    prev_history_loss_avg = curr_loss_history
                history_loss_avg.reset_states()

        # every 10 epochs
        if (N_EPOCHS < 100) or (epoch % (N_EPOCHS // 100) == 0):
            # Compute validation loss and monitor it on tensoboard
            test_loss.reset_states()
            for elt in ds_val_dist:
                test_loss.update_state(distributed_test_step(elt))
            step_int = int(loss_per_epoch * count_step * batch_size / n_train)
            with test_summary_writer.as_default():
                tf.summary.scalar('loss', test_loss.result(), step=step_int)
            print("Epoch {:03d}: Train Loss: {:.3f} Val Loss: {:03f}".format(
                epoch, epoch_loss_avg.result(), test_loss.result()))
            # Generate some samples and visualize them on tensorboard
            with mirrored_strategy.scope():
                samples = flow.sample(32)
            if args.img_type == "image":
                samples += 0.5
            samples = samples.numpy().reshape([32] + args.data_shape)
            try:
                figure = image_grid(samples, args.data_shape, args.img_type,
                                    sampling_rate=args.sampling_rate, fmin=args.fmin, fmax=args.fmax)
                with train_summary_writer.as_default():
                    tf.summary.image("32 generated samples", plot_to_image(figure),
                                     max_outputs=50, step=epoch)
            except IndexError:
                with train_summary_writer.as_default():
                    tf.summary.text(name="display error",
                                    data="Impossible to display spectrograms because of NaN values",
                                    step=epoch)

            # If minimum validation loss is reached, save model
            curr_val_loss = test_loss.result()
            if curr_val_loss < min_val_loss:
                save_path = manager.save()
                print("Model Saved at {}".format(save_path))
                min_val_loss = curr_val_loss

    save_path = manager.save()
    print("Model Saved at {}".format(save_path))
    training_time = time.time() - t0
    return training_time, save_path


def main(args):
    # miscellaneous paramaters
    if args.dataset == 'mnist':
        args.data_shape = [32, 32, 1]
        args.img_type = "image"
        args.preprocessing_glow = None
    elif args.dataset == 'cifar10':
        args.data_shape = [32, 32, 3]
        args.img_type = "image"
        args.preprocessing_glow = None
    else:
        args.data_shape = [args.height, args.width, 1]
        args.dataset = os.path.abspath(args.dataset)
        args.img_type = "melspec"
        args.preprocessing_glow = "melspec"
        args.instrument = os.path.split(args.dataset)[-1]

    # output directory
    if args.restore is not None:
        abs_restore_path = os.path.abspath(args.restore)
    if args.output == 'trained_flow':
        if args.dataset != 'mnist' and args.dataset != 'cifar10':
            dataset = args.instrument
        else:
            dataset = args.dataset
        if args.model == 'glow':
            output_dirname = args.model + '_' + dataset + '_' + str(args.L) + '_' + \
                str(args.K) + '_' + str(args.n_filters) + '_' + str(args.batch_size)
        elif args.model == 'flowpp':
            output_dirname = args.model + '_' + dataset + '_' + str(args.n_components) + '_' + \
                str(args.n_blocks_flow) + '_' + str(args.filters) + '_' + str(args.batch_size)

        if args.use_logit:
            output_dirname += '_logit'
        if args.restore is not None:
            output_dirname += '_ctd'

        output_dirpath = os.path.join(args.output, output_dirname)
    else:
        output_dirpath = args.output
    try:
        os.mkdir(output_dirpath)
        os.chdir(output_dirpath)
    except FileExistsError:
        os.chdir(output_dirpath)
    log_file = open('out.log', 'w')
    if args.debug is False:
        sys.stdout = log_file

    print("TensorFlow version: {}".format(tf.__version__))
    print("Eager execution: {}".format(tf.executing_eagerly()))

    # Set up tensorboard
    train_summary_writer, test_summary_writer = setUp_tensorboard()

    # Distributed Strategy
    mirrored_strategy = tf.distribute.MirroredStrategy()
    print("Number of devices: {}".format(
        mirrored_strategy.num_replicas_in_sync))

    # Load Dataset
    if args.model == 'flowpp':
        preprocessing_dataloader = False
    else:
        preprocessing_dataloader = True

    if (args.dataset == "mnist") or (args.dataset == "cifar10"):
        ds, ds_val, ds_dist, ds_val_dist, minibatch, _, _ = data_loader.load_data(dataset=args.dataset, batch_size=args.batch_size,
                                                                                  mirrored_strategy=mirrored_strategy, use_logit=args.use_logit,
                                                                                  noise=args.noise, alpha=args.alpha, preprocessing=preprocessing_dataloader)
        args.test_batch_size = 5000
    else:
        ds, ds_val, ds_dist, ds_val_dist, minibatch, n_train, n_test = data_loader.load_melspec_ds(args.dataset + '/train', args.dataset + '/test',
                                                                                                   batch_size=args.batch_size,
                                                                                                   reshuffle=True,
                                                                                                   mirrored_strategy=mirrored_strategy)
        args.test_batch_size = args.batch_size
        args.n_train = n_train
        args.n_test = n_test
    # Display original images
    with train_summary_writer.as_default():
        sample = list(ds.take(1).as_numpy_iterator())[0]
        sample = sample[:32]
        figure = image_grid(sample, args.data_shape, args.img_type,
                            sampling_rate=args.sampling_rate, fmin=args.fmin, fmax=args.fmax)
        tf.summary.image("original images", plot_to_image(figure), max_outputs=1, step=0)

    # Build Flow
    if args.model == 'glow':
        flow = flow_builder.build_glow(minibatch, args.data_shape, L=args.L, K=args.K, n_filters=args.n_filters,
                                       l2_reg=args.l2_reg, mirrored_strategy=mirrored_strategy, learntop=args.learntop,
                                       preprocessing_bij=args.preprocessing_glow)
    elif args.model == 'flowpp':
        flow = flow_builder.build_flowpp(minibatch, args.data_shape, n_components=args.n_components,
                                         n_blocks_flow=args.n_blocks_flow, n_blocks_dequant=args.n_blocks_dequant,
                                         filters=args.filters, dropout_p=args.dropout_p, heads=args.heads,
                                         mirrored_strategy=mirrored_strategy)
    else:
        raise ValueError("model should be glow or flowpp")

    # Set up optimizer
    optimizer = setUp_optimizer(mirrored_strategy, args)

    # Set up checkpoint
    ckpt, manager, manager_issues = setUp_checkpoint(
        mirrored_strategy, args, flow, optimizer)

    # restore
    if args.restore is not None:
        with mirrored_strategy.scope():
            if args.latest:
                checkpoint_restore_path = tf.train.latest_checkpoint(abs_restore_path)
                assert checkpoint_restore_path is not None, abs_restore_path
            else:
                checkpoint_restore_path = abs_restore_path
            status = ckpt.restore(checkpoint_restore_path)
            status.assert_existing_objects_matched()
            assert optimizer.iterations > 0
            print("Model Restored from {}".format(abs_restore_path))

    # Display parameters
    params_dict = vars(args)
    template = ''
    for k, v in params_dict.items():
        template += '{} = {} \n\t '.format(k, v)
    print(template)

    with mirrored_strategy.scope():
        print("flow sample shape: ", flow.sample(1).shape)

    total_trainable_variables = utils.total_trainable_variables(flow)
    print("Total Trainable Variables: ", total_trainable_variables)

    with train_summary_writer.as_default():
        tf.summary.text(name='Parameters',
                        data=tf.constant(template), step=0)
        tf.summary.text(name="Total Trainable Variables",
                        data=tf.constant(str(total_trainable_variables)), step=0)

    # Train
    training_time, _ = train(mirrored_strategy, args, flow, optimizer, ds_dist, ds_val_dist,
                             manager, manager_issues, train_summary_writer, test_summary_writer)
    print("Training time: ", np.round(training_time, 2), ' seconds')

    log_file.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Train Flow model')
    # dataset parameters
    parser.add_argument('--dataset', type=str, default="mnist",
                        help="mnist or cifar10 or directory to tfrecords")

    # Spectrograms Parameters
    parser.add_argument("--height", type=int, default=96)
    parser.add_argument("--width", type=int, default=64)
    parser.add_argument("--sampling_rate", type=int, default=16000)
    parser.add_argument("--fmin", type=int, default=125)
    parser.add_argument("--fmax", type=int, default=7600)

    # Output and Restore Directory
    parser.add_argument('--output', type=str, default='trained_flow',
                        help='output dirpath for savings')
    parser.add_argument('--restore', type=str, default=None,
                        help='directory of saved weights (optional)')
    parser.add_argument('--latest', action="store_true", help="Restore latest checkpoint from restore directory")
    parser.add_argument('--debug', action="store_true")

    # Model hyperparameters
    parser.add_argument('--model', default='glow', type=str, help='glow or flowpp')
    parser.add_argument("--learntop", action="store_true",
                        help="learnable prior distribution")
    # Glow hyperparameters
    parser.add_argument('--L', default=3, type=int,
                        help='Depth level')
    parser.add_argument('--K', type=int, default=32,
                        help="Number of Step of Flow in each Block")
    parser.add_argument('--n_filters', type=int, default=512,
                        help="number of filters in the Convolutional Network")
    parser.add_argument('--l2_reg', type=float, default=None,
                        help="L2 regularization for the coupling layer")
    # Flow++ hyperparameters
    parser.add_argument('--n_components', type=int, default=32)
    parser.add_argument('--n_blocks_flow', type=int, default=10)
    parser.add_argument('--n_blocks_dequant', type=int, default=2)
    parser.add_argument('--filters', type=int, default=96)
    parser.add_argument('--dropout_p', type=float, default=0.2)
    parser.add_argument('--heads', type=int, default=4)

    # Optimization parameters
    parser.add_argument('--n_epochs', type=int, default=100,
                        help='number of epochs to train')
    parser.add_argument("--optimizer", type=str,
                        default="adamax", help="adam or adamax")
    parser.add_argument('--batch_size', type=int, default=256)
    parser.add_argument('--learning_rate', type=float, default=0.001)
    parser.add_argument('--clipvalue', type=float, default=None,
                        help="Clip value for Adam optimizer")
    parser.add_argument('--clipnorm', type=float, default=None,
                        help='Clip norm for Adam optimize')

    # preprocessing parameters
    parser.add_argument('--use_logit', action="store_true",
                        help="Either to use logit function to preprocess the data")
    parser.add_argument('--alpha', type=float, default=10**(-6),
                        help='preprocessing parameter: x = logit(alpha + (1 - alpha) * z / 256.). Only if use logit')
    parser.add_argument('--noise', type=float, default=None, help='noise level for BASIS separation')

    args = parser.parse_args()

    main(args)
