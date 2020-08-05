import numpy as np
import tensorflow as tf
from ncsn import score_network
from pipeline import data_loader
import tensorflow_datasets as tfds
import argparse
import time
from train_utils import *
import os
tfk = tf.keras


def get_uncompiled_model(args):
    # inputs
    perturbed_X = tfk.Input(shape=args.data_shape,
                            dtype=tf.float32, name="perturbed_X")
    sigma_idx = tfk.Input(shape=[], dtype=tf.int32, name="sigma_idx")
    # outputs
    outputs = score_network.CondRefineNetDilated(args.data_shape, args.n_filters,
                                                 args.num_classes, args.use_logit)([perturbed_X, sigma_idx])
    # model
    model = tfk.Model(inputs=[perturbed_X, sigma_idx],
                      outputs=outputs, name="ScoreNetwork")

    return model


def main(args):

    sigmas_np = np.logspace(np.log(args.sigma1) / np.log(10),
                            np.log(args.sigmaL) / np.log(10),
                            num=args.num_classes)

    sigmas_tf = tf.constant(sigmas_np, dtype=tf.float32)

    # Print parameters
    print("EVALUATION PARAMETERS")
    params_dict = vars(args)
    template = '\t '
    for k, v in params_dict.items():
        template += '{} = {} \n\t '.format(k, v)
    print(template)
    print("_" * 100)

    sigmas_np = np.logspace(np.log(args.sigma1) / np.log(10),
                            np.log(args.sigmaL) / np.log(10),
                            num=args.num_classes)

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

    mirrored_strategy = None

    # Restore Model
    abs_restore_path = os.path.abspath(args.RESTORE)
    model = get_uncompiled_model(args)
    optimizer = setUp_optimizer(mirrored_strategy, args)
    model.compile(optimizer=optimizer, loss=tfk.losses.MeanSquaredError())
    model.load_weights(abs_restore_path)
    print("Weights loaded")

    # Load and Preprocess Dataset
    if (args.dataset == "mnist") or (args.dataset == "cifar10"):
        datasets, info = tfds.load(
            name=args.dataset, with_info=True, as_supervised=False)
        ds_train, ds_test = datasets['train'], datasets['test']
        ds_train = ds_train.map(lambda x: x['image'])
        ds_test = ds_test.map(lambda x: x['image'])
        if args.dataset == 'mnist':
            ds_train = ds_train.map(lambda x: tf.pad(x, tf.constant([[2, 2], [2, 2], [0, 0]])))
            ds_test = ds_test.map(lambda x: tf.pad(x, tf.constant([[2, 2], [2, 2], [0, 0]])))

        args.dataset_maxval = 256.

    else:
        ds_train, ds_test, _, n_train, n_test = data_loader.load_melspec_ds(args.dataset + '/train', args.dataset + '/test',
                                                                            reshuffle=True, batch_size=None, mirrored_strategy=None)
        args.dataset_maxval = 100.1
        args.n_train = n_train
        args.n_test = n_test

    BUFFER_SIZE = 10000
    BATCH_SIZE = args.batch_size

    def preprocess(image):
        sigma_idx = tf.random.uniform(shape=(), maxval=10, dtype=tf.int32)
        used_sigma = tf.gather(params=sigmas_tf, indices=sigma_idx)
        X = tf.cast(image, tf.float32) / args.dataset_maxval
        if args.img_type == 'image':
            X += tf.random.uniform(X.shape, minval=0., maxval=(1. / 256.))
        if args.use_logit:
            X = tf.math.log(X) - tf.math.log(1. - X)
        perturbed_X = X + tf.random.normal(X.shape) * used_sigma
        inputs = {'perturbed_X': perturbed_X, 'sigma_idx': sigma_idx}
        target = -(perturbed_X - X) / (used_sigma ** 2)
        sample_weight = used_sigma ** 2
        return inputs, target, sample_weight

    train_dataset = ds_train.map(preprocess, num_parallel_calls=tf.data.experimental.AUTOTUNE)
    train_dataset = train_dataset.cache().shuffle(BUFFER_SIZE).batch(BATCH_SIZE)
    train_dataset = train_dataset.prefetch(tf.data.experimental.AUTOTUNE)
    eval_dataset = ds_test.map(preprocess, num_parallel_calls=tf.data.experimental.AUTOTUNE)
    eval_dataset = eval_dataset.cache().shuffle(BUFFER_SIZE).batch(BATCH_SIZE)
    eval_dataset = eval_dataset.prefetch(tf.data.experimental.AUTOTUNE)

    print("Start Evaluation on Training Set")
    t0 = time.time()
    model.evaluate(train_dataset)
    print("Done. Duration: {} seconds".format(round(time.time() - t0, 2)))

    print("Start Evaluation on Testing Set")
    t0 = time.time()
    model.evaluate(eval_dataset)
    print("Done. Duration: {} seconds".format(round(time.time() - t0, 2)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Evaluate NCSN model')

    parser.add_argument('RESTORE', type=str, default=None,
                        help='directory of saved weights')

    # dataset parameters
    parser.add_argument('--dataset', type=str, default="mnist",
                        help="mnist or cifar10 or directory to tfrecords")

    # Spectrograms Parameters
    parser.add_argument("--height", type=int, default=96)
    parser.add_argument("--width", type=int, default=64)

    # Model hyperparameters
    parser.add_argument("--n_filters", type=int, default=64,
                        help='number of filters in the Score Network')
    parser.add_argument("--sigma1", type=float, default=1.)
    parser.add_argument("--sigmaL", type=float, default=0.01)
    parser.add_argument("--num_classes", type=int, default=10)

    # Preprocessing parameters
    parser.add_argument("--use_logit", action="store_true")

    # Optimization parameters
    parser.add_argument("--optimizer", type=str,
                        default="adam", help="adam or adamax")
    parser.add_argument('--batch_size', type=int, default=128)
    parser.add_argument('--learning_rate', type=float, default=0.001)
    parser.add_argument('--clipvalue', type=float, default=None,
                        help="Clip value for Adam optimizer")
    parser.add_argument('--clipnorm', type=float, default=None,
                        help='Clip norm for Adam optimize')

    args = parser.parse_args()

    main(args)