import tensorflow as tf
import tensorflow_datasets as tfds
from .preprocessing import load_tf_records
import os
import re


def load_toydata(dataset='mnist', batch_size=256, use_logit=False, noise=None,
                 alpha=0.01, mirrored_strategy=None, reshuffle=True, preprocessing=True,
                 model='flow', num_classes=10):

    assert model == 'flow' or model == 'ncsn'

    if dataset == 'mnist':
        data_shape = (32, 32, 1)
    elif dataset == 'cifar10':
        data_shape = (32, 32, 3)
    else:
        raise ValueError("dataset should be mnist or cifar10")

    buffer_size = 2048
    global_batch_size = batch_size
    ds = tfds.load(dataset, split='train', shuffle_files=True)
    n_train = len(list(ds.as_numpy_iterator()))
    # Build your input pipeline
    ds = ds.map(lambda x: x['image'])
    ds = ds.map(lambda x: tf.cast(x, tf.float32))
    if dataset == 'mnist':
        ds = ds.map(lambda x: tf.pad(x, tf.constant([[2, 2], [2, 2], [0, 0]])))

    if preprocessing and model == 'flow':
        ds = ds.map(lambda x: x / 256. - 0.5)

    if noise is not None:
        ds = ds.map(lambda x: x + tf.random.normal(shape=data_shape) * noise)
    ds = ds.map(lambda x: x + tf.random.uniform(shape=data_shape,
                                                minval=0., maxval=1. / 256.))
    if use_logit:
        ds = ds.map(lambda x: alpha + (1 - alpha) * x)
        ds = ds.map(lambda x: tf.math.log(x / (1 - x)))

    if model == 'ncsn':
        ds = ds.map(lambda x: x / 256. + tf.random.uniform(shape=data_shape, minval=0., maxval=1. / 256.))
        ds = ds.map(lambda x: (x, tf.random.uniform((), 0, num_classes, dtype=tf.int32)))

    ds = ds.shuffle(buffer_size, reshuffle_each_iteration=reshuffle)
    ds = ds.batch(global_batch_size, drop_remainder=True)
    minibatch = list(ds.take(1))[0]

    # Validation Set
    ds_val = tfds.load(dataset, split='test', shuffle_files=True)
    ds_val = ds_val.map(lambda x: x['image'])
    ds_val = ds_val.map(lambda x: tf.cast(x, tf.float32))
    if dataset == 'mnist':
        ds_val = ds_val.map(lambda x: tf.pad(
            x, tf.constant([[2, 2], [2, 2], [0, 0]])))

    if preprocessing and model == 'flow':
        ds_val = ds_val.map(lambda x: x / 256. - 0.5)
        ds_val = ds_val.map(lambda x: x + tf.random.uniform(shape=data_shape, minval=0., maxval=1. / 256.))

    if noise is not None:
        ds_val = ds_val.map(lambda x: x + tf.random.normal(shape=data_shape) * noise)

    if use_logit:
        ds_val = ds_val.map(lambda x: alpha + (1 - alpha) * x)
        ds_val = ds_val.map(lambda x: tf.math.log(x / (1 - x)))

    if model == 'ncsn':
        ds_val = ds_val.map(lambda x: x / 256. + tf.random.uniform(shape=data_shape, minval=0., maxval=1. / 256.))
        ds_val = ds_val.map(lambda x: (x, tf.random.uniform((1,), 0, num_classes, dtype=tf.int32)))

    ds_val = ds_val.batch(5000)

    if mirrored_strategy is not None:
        ds_dist = mirrored_strategy.experimental_distribute_dataset(ds)
        ds_val_dist = mirrored_strategy.experimental_distribute_dataset(ds_val)
        return ds, ds_val, ds_dist, ds_val_dist, minibatch, n_train

    else:
        return ds, ds_val, minibatch, n_train


def get_mixture(dataset='mnist', n_mixed=10, use_logit=False, alpha=None, noise=0.1, mirrored_strategy=None):

    if dataset == 'mnist':
        data_shape = [n_mixed, 32, 32, 1]
    elif dataset == 'cifar10':
        data_shape = [n_mixed, 32, 32, 3]
    else:
        raise ValueError("args.dataset should be mnist or cifar10")

    ds, _, minibatch = load_toydata(dataset, n_mixed, use_logit, alpha, noise, mirrored_strategy, preprocessing=False)

    ds1 = ds.take(1)
    ds2 = ds.take(1)
    for gt1, gt2 in zip(ds1, ds2):
        gt1, gt2 = gt1, gt2

    gt1 = gt1 / 256. - .5 + tf.random.uniform(data_shape, minval=0., maxval=1. / 256.)
    gt2 = gt2 / 256. - .5 + tf.random.uniform(data_shape, minval=0., maxval=1. / 256.)
    mixed = (gt1 + gt2) / 2.

    # x1 = tf.random.uniform(data_shape, minval=-.5, maxval=.5)
    # x2 = tf.random.uniform(data_shape, minval=-.5, maxval=.5)
    x1 = tf.random.normal(data_shape)
    x2 = tf.random.normal(data_shape)

    return mixed, x1, x2, gt1, gt2, minibatch


def load_melspec_ds(dirpath, batch_size=256, reshuffle=True, mirrored_strategy=None):

    melspec_files = []
    dirpath = os.path.abspath(dirpath)
    for root, dirs, files in os.walk(dirpath):
        current_path = os.path.join(dirpath, root)
        if len(files) > 0:
            melspec_files += [os.path.join(current_path, f) for f in files if re.match(".*(.)tfrecord$", f)]

    buffer_size = 2048
    ds = load_tf_records(melspec_files)
    ds = ds.shuffle(buffer_size, reshuffle_each_iteration=False)
    ds = ds.map(lambda x: tf.expand_dims(x, axis=-1))
    ds_size = len(list(ds.as_numpy_iterator()))
    # split into training and testing_set
    ds_test = ds.take(ds_size * 20 // 100)
    ds_train = ds.skip(ds_size * 20 // 100)
    n_train = ds_size - (ds_size * 20 // 100)

    ds_train = ds_train.batch(batch_size, drop_remainder=True)
    minibatch = list(ds_train.take(1))[0]

    ds_test = ds_test.batch(batch_size, drop_remainder=True)

    if mirrored_strategy is not None:
        ds_train_dist = mirrored_strategy.experimental_distribute_dataset(ds_train)
        ds_test_dist = mirrored_strategy.experimental_distribute_dataset(ds_test)
        return ds_train, ds_test, ds_train_dist, ds_test_dist, minibatch, n_train

    else:
        return ds_train, ds_test, minibatch, n_train
