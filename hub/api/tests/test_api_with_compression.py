from io import BytesIO
from hub.constants import KB, MB
from hub.util.exceptions import (
    SampleCompressionError,
    TensorMetaMissingRequiredValue,
    TensorMetaMutuallyExclusiveKeysError,
    UnsupportedCompressionError,
)
import pytest
from hub.core.tensor import Tensor
from hub.tests.common import TENSOR_KEY, assert_images_close
from hub.tests.dataset_fixtures import (
    enabled_datasets,
    enabled_persistent_dataset_generators,
)
import numpy as np

import hub
from hub.core.dataset import Dataset
from miniaudio import mp3_read_file_f32, flac_read_file_f32, wav_read_file_f32  # type: ignore


def _populate_compressed_samples(tensor: Tensor, cat_path, flower_path, count=1):
    for _ in range(count):
        tensor.append(hub.read(cat_path))
        tensor.append(hub.read(flower_path))
        tensor.append(np.ones((100, 100, 4), dtype="uint8"))
        tensor.append(
            np.ones((100, 100, 4), dtype=int).tolist()
        )  # test safe downcasting of python scalars

        tensor.extend(
            [
                hub.read(flower_path),
                hub.read(cat_path),
            ]
        )


@enabled_datasets
def test_populate_compressed_samples(ds: Dataset, cat_path, flower_path):
    images = ds.create_tensor(
        TENSOR_KEY, htype="image", sample_compression="png", max_chunk_size=2 * MB
    )

    assert images.meta.dtype == "uint8"
    assert images.meta.sample_compression == "png"

    _populate_compressed_samples(images, cat_path, flower_path)

    expected_shapes = [
        (900, 900, 3),
        (513, 464, 4),
        (100, 100, 4),
        (100, 100, 4),
        (513, 464, 4),
        (900, 900, 3),
    ]

    assert len(images) == 6

    for img, exp_shape in zip(images, expected_shapes):
        arr = img.numpy()
        assert arr.shape == exp_shape
        assert arr.dtype == "uint8"

    assert images.shape == (6, None, None, None)
    assert images.shape_interval.lower == (6, 100, 100, 3)
    assert images.shape_interval.upper == (6, 900, 900, 4)


@enabled_datasets
def test_iterate_compressed_samples(ds: Dataset, cat_path, flower_path):
    images = ds.create_tensor(TENSOR_KEY, htype="image", sample_compression="png")

    assert images.meta.dtype == "uint8"
    assert images.meta.sample_compression == "png"

    _populate_compressed_samples(images, cat_path, flower_path)

    expected_shapes = [
        (900, 900, 3),
        (513, 464, 4),
        (100, 100, 4),
        (100, 100, 4),
        (513, 464, 4),
        (900, 900, 3),
    ]

    assert len(images) == len(expected_shapes)
    for image, expected_shape in zip(images, expected_shapes):
        x = image.numpy()

        assert (
            type(x) == np.ndarray
        ), "Check is necessary in case a `PIL` object is returned instead of an array."
        assert x.shape == expected_shape
        assert x.dtype == "uint8"


@enabled_datasets
def test_uncompressed(ds: Dataset):
    images = ds.create_tensor(TENSOR_KEY, sample_compression=None)

    images.append(np.ones((100, 100, 100)))
    images.extend(np.ones((3, 101, 2, 1)))
    ds.clear_cache()
    np.testing.assert_array_equal(images[0].numpy(), np.ones((100, 100, 100)))
    np.testing.assert_array_equal(images[1:4].numpy(), np.ones((3, 101, 2, 1)))


def test_byte_sample_compression(memory_ds):
    with memory_ds as ds:
        ds.create_tensor("xyz", sample_compression="lz4")
        for i in range(10):
            ds.xyz.append(i * np.ones((100, 100, 100)))

    for i in range(10):
        np.testing.assert_array_equal(ds.xyz[i].numpy(), i * np.ones((100, 100, 100)))


@pytest.mark.xfail(raises=SampleCompressionError, strict=True)
@pytest.mark.parametrize(
    "bad_shape",
    [
        # raises OSError: cannot write mode LA as JPEG
        (100, 100, 2),
        # raises OSError: cannot write mode RGBA as JPE
        (100, 100, 4),
    ],
)
def test_jpeg_bad_shapes(memory_ds: Dataset, bad_shape):
    # jpeg allowed shapes:
    # ---------------------
    # (100) works!
    # (100,) works!
    # (100, 100) works!
    # (100, 100, 1) works!
    # (100, 100, 2) raises   | OSError: cannot write mode LA as JPEG
    # (100, 100, 3) works!
    # (100, 100, 4) raises   | OSError: cannot write mode RGBA as JPEG
    # (100, 100, 5) raises   | TypeError: Cannot handle this data type: (1, 1, 5), |u1
    # (100, 100, 100) raises | TypeError: Cannot handle this data type: (1, 1, 100), |u1

    tensor = memory_ds.create_tensor(TENSOR_KEY, sample_compression="jpeg")
    tensor.append(np.ones(bad_shape, dtype="uint8"))


def test_compression_aliases(memory_ds: Dataset):
    tensor = memory_ds.create_tensor("jpeg_tensor", sample_compression="jpeg")
    assert tensor.meta.sample_compression == "jpeg"

    tensor = memory_ds.create_tensor("jpg_tensor", sample_compression="jpg")
    assert tensor.meta.sample_compression == "jpeg"


@pytest.mark.xfail(raises=UnsupportedCompressionError, strict=True)
def test_unsupported_compression(memory_ds: Dataset):
    memory_ds.create_tensor(TENSOR_KEY, sample_compression="bad_compression")
    # TODO: same tests but with `dtype`


@pytest.mark.xfail(raises=TensorMetaMissingRequiredValue, strict=True)
def test_missing_sample_compression_for_image(memory_ds: Dataset):
    memory_ds.create_tensor("tensor", htype="image")


@pytest.mark.xfail(raises=TensorMetaMutuallyExclusiveKeysError, strict=True)
def test_sample_chunk_compression_mutually_exclusive(memory_ds: Dataset):
    memory_ds.create_tensor(
        "tensor", htype="image", sample_compression="png", chunk_compression="lz4"
    )


def test_chunkwise_compression(memory_ds, cat_path, flower_path):
    ds = memory_ds
    im_ct = 5
    chunk_size = 600 * KB
    with ds:
        images = ds.create_tensor(
            "images", htype="image", chunk_compression="jpg", max_chunk_size=chunk_size
        )
        images.extend([hub.read(cat_path)] * im_ct)
        expected_arr = np.random.randint(0, 10, (500, 450, 3)).astype("uint8")
        images.append(expected_arr)
        images.extend([hub.read(cat_path)] * im_ct)
        expected_img = np.array(hub.read(cat_path))
    ds.clear_cache()
    for i, img in enumerate(images):
        if i == im_ct:
            assert_images_close(img.numpy(), expected_arr)
        else:
            assert_images_close(img.numpy(), expected_img)
    with ds:
        images = ds.create_tensor(
            "images2", htype="image", chunk_compression="png", max_chunk_size=chunk_size
        )
        images.extend([hub.read(flower_path)] * im_ct)
        expected_arr = np.random.randint(0, 256, (200, 250, 4)).astype("uint8")
        images.append(expected_arr)
        images.extend([hub.read(flower_path)] * im_ct)
        expected_img = np.array(hub.read(flower_path))
    ds.clear_cache()
    for i, img in enumerate(images):
        if i == im_ct:
            assert_images_close(img.numpy(), expected_arr)
        else:
            assert_images_close(img.numpy(), expected_img)
    with ds:
        labels = ds.create_tensor(
            "labels", chunk_compression="lz4", max_chunk_size=chunk_size
        )
        data = [
            np.random.randint(0, 256, (150, 150)).astype("uint8") for _ in range(20)
        ]
        labels.extend(data)
    ds.clear_cache()
    for row, label in zip(data, labels):
        np.testing.assert_array_equal(row, label.numpy())

    data = np.random.randint(0, 256, (5, 1500, 1500)).astype("uint8")
    with ds:
        ds.labels.extend(data)  # type: ignore
    ds.clear_cache()
    assert len(ds.labels) == 25
    for i in range(5):
        np.testing.assert_array_equal(data[i], ds.labels[20 + i].numpy())


@enabled_datasets
@pytest.mark.parametrize("compression", hub.compression.AUDIO_COMPRESSIONS)
def test_audio(ds: Dataset, compression, audio_paths):
    path = audio_paths[compression]
    if path.endswith(".mp3"):
        audio = mp3_read_file_f32(path)
    elif path.endswith(".flac"):
        audio = flac_read_file_f32(path)
    elif path.endswith(".wav"):
        audio = wav_read_file_f32(path)
    arr = np.frombuffer(audio.samples, dtype=np.float32).reshape(
        audio.num_frames, audio.nchannels
    )
    ds.create_tensor("audio", htype="audio", sample_compression=compression)
    with ds:
        for _ in range(10):
            ds.audio.append(hub.read(path))  # type: ignore
    for i in range(10):
        np.testing.assert_array_equal(ds.audio[i].numpy(), arr)  # type: ignore


def test_tracked_sizes(memory_ds: Dataset, cat_path, flower_path):
    from PIL import Image

    cat_shape = (900, 900, 3)
    flower_shape = (513, 464, 4)
    ones_shape = (100, 100, 4)

    shapes = [cat_shape, flower_shape, ones_shape]

    with open(flower_path, "rb") as f:  # already png
        flower_png_size = len(f.read())

    for path in (cat_path, "ones"):  # requires converting to png to get size
        if path == "ones":
            image = Image.fromarray(np.ones(ones_shape, dtype="uint8"))
        else:
            hub_image = hub.read(path)
            image = Image.fromarray(
                hub_image.array
            )  # size is different from array and from file. need to use array here for consistency.

        with BytesIO() as output:
            image.save(output, "PNG")
            if path == cat_path:
                cat_png_size = len(output.getvalue())
            else:
                ones_png_size = len(output.getvalue())

    image = memory_ds.create_tensor("image", sample_compression="png")
    _populate_compressed_samples(image, cat_path, flower_path)

    assert (
        image.num_compressed_bytes
        == (cat_png_size + flower_png_size + ones_png_size) * 2
    )
    assert (
        image.num_uncompressed_bytes
        == sum([np.prod(shape).item() for shape in shapes]) * 2
    )
    assert image.chunk_engine._get_num_compressed_bytes() == image.num_compressed_bytes
    assert (
        image.chunk_engine._get_num_uncompressed_bytes() == image.num_uncompressed_bytes
    )

    chunk_size = 600 * KB
    image = memory_ds.create_tensor(
        "ch_image", htype="image", chunk_compression="jpg", max_chunk_size=chunk_size
    )
    image.extend([hub.read(cat_path)] * 5)

    assert image.num_uncompressed_bytes == np.prod(cat_shape) * 5


@enabled_persistent_dataset_generators
def test_tracked_sizes_persistence(ds_generator: Dataset, flower_path):
    ds = ds_generator()
    flower_shape = (513, 464, 4)
    with open(flower_path, "rb") as f:
        flower_png_size = len(f.read())

    image = ds.create_tensor("image", sample_compression="png")
    image.append(hub.read(flower_path))

    assert image.num_compressed_bytes == flower_png_size
    assert image.num_uncompressed_bytes == np.prod(flower_shape)

    ds = ds_generator()
    image = ds["image"]

    assert image.num_compressed_bytes == flower_png_size
    assert image.num_uncompressed_bytes == np.prod(flower_shape)
