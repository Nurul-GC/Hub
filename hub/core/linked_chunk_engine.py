import hub
from hub.core.chunk.base_chunk import BaseChunk
from hub.core.chunk_engine import ChunkEngine
from hub.core.compression import _read_video_shape, _decompress_video
from hub.core.index.index import Index
from hub.core.link_creds import LinkCreds
from hub.core.linked_sample import LinkedSample
from hub.core.meta.encode.creds import CredsEncoder
from hub.core.storage import LRUCache
from hub.core.tensor_link import read_linked_sample
from hub.util.exceptions import ReadOnlyModeError
from hub.util.keys import get_creds_encoder_key
from hub.util.link import (
    get_path_creds_key,
    save_link_creds,
    warn_missing_managed_creds,
)
from hub.util.video import normalize_index
import numpy as np
from typing import Optional, Dict, Any, Tuple


class LinkedChunkEngine(ChunkEngine):
    def __init__(
        self,
        key: str,
        cache: LRUCache,
        version_state: Dict[str, Any],
        link_creds: LinkCreds,
        meta_cache: LRUCache = None,
    ):
        super().__init__(key, cache, version_state, meta_cache)
        self.link_creds = link_creds
        self._creds_encoder: Optional[CredsEncoder] = None
        self._creds_encoder_commit_id: Optional[str] = None

    @property
    def creds_encoder(self):
        commit_id = self.commit_id
        if self._creds_encoder is None or self._creds_encoder_commit_id != commit_id:
            commit_id = self.commit_id
            key = get_creds_encoder_key(self.key, commit_id)
            if not self.creds_encoder_exists:
                enc = CredsEncoder()
                try:
                    self.meta_cache[key] = enc
                except ReadOnlyModeError:
                    pass
            else:
                enc = self.meta_cache.get_hub_object(key, CredsEncoder)
            self._creds_encoder = enc
            self._creds_encoder_commit_id = commit_id
            self.meta_cache.register_hub_object(key, enc)
        return self._creds_encoder

    @property
    def creds_encoder_exists(self):
        commit_id = self.commit_id
        if (
            self._creds_encoder is not None
            and self._creds_encoder_commit_id == commit_id
        ):
            return True
        try:
            key = get_creds_encoder_key(self.key, commit_id)
            self.meta_cache[key]
            return True
        except KeyError:
            return False

    @property
    def is_data_cachable(self):
        return False

    def linked_sample(self, global_sample_index: int) -> LinkedSample:
        creds_encoder = self.creds_encoder
        sample_path = self.get_path(global_sample_index)
        sample_creds_encoded = creds_encoder.get_encoded_creds_key(global_sample_index)
        sample_creds_key = self.link_creds.get_creds_key(sample_creds_encoded)
        return LinkedSample(sample_path, sample_creds_key)

    def get_video_url(self, global_sample_index):
        creds_encoder = self.creds_encoder
        sample_path = self.get_path(global_sample_index)
        sample_creds_encoded = creds_encoder.get_encoded_creds_key(global_sample_index)
        sample_creds_key = self.link_creds.get_creds_key(sample_creds_encoded)
        storage = None
        if sample_path.startswith(("gcs://", "gcp://", "s3://")):
            provider_type = "s3" if sample_path.startswith("s3://") else "gcs"
            storage = self.link_creds.get_storage_provider(
                sample_creds_key, provider_type
            )
            url = storage.get_presigned_url(sample_path, full=True)
        else:
            url = sample_path
        return url

    def get_video_sample(self, global_sample_index, index):
        url = self.get_video_url(global_sample_index)
        squeeze = isinstance(index, int)
        shape = _read_video_shape(url)
        sub_index = index.values[1].value if len(index.values) > 1 else None  # type: ignore
        start, stop, step, reverse = normalize_index(sub_index, shape[0])
        video_sample = _decompress_video(
            url,
            start,
            stop,
            step,
            reverse,
        )
        if squeeze:
            video_sample.squeeze(0)
        return video_sample

    def get_basic_sample(self, global_sample_index, index, fetch_chunks=False):
        sample = self.get_hub_read_sample(global_sample_index)
        if sample is None:
            return np.ones((0,))
        return sample.array[tuple(entry.value for entry in index.values[1:])]

    def get_path(self, global_sample_index) -> str:
        return super().get_basic_sample(
            global_sample_index, Index(global_sample_index)
        )[0]

    def get_hub_read_sample(self, global_sample_index):
        creds_encoder = self.creds_encoder
        sample_path = self.get_path(global_sample_index)
        if not sample_path:
            return None
        sample_creds_encoded = creds_encoder.get_encoded_creds_key(global_sample_index)
        sample_creds_key = self.link_creds.get_creds_key(sample_creds_encoded)
        return read_linked_sample(sample_path, sample_creds_key, self.link_creds, False)

    @property
    def verify(self):
        return self.tensor_meta.is_link and self.tensor_meta.verify

    def check_each_sample(self, samples):
        link_creds = self.link_creds
        verified_samples = []
        for i, sample in enumerate(samples):
            if isinstance(sample, hub.core.tensor.Tensor) and sample.is_link:
                sample = sample._linked_sample()
                samples[i] = sample
            elif not isinstance(sample, LinkedSample) and sample is not None:
                raise TypeError(
                    f"Expected LinkedSample, got {type(sample)} instead. Use hub.link() to link samples."
                )

            path, creds_key = get_path_creds_key(sample)

            # verifies existence of creds_key
            link_creds.get_encoding(creds_key, path)

            if sample is None or sample.path == "":
                verified_samples.append(sample)
            else:
                verified_samples.append(
                    read_linked_sample(
                        sample.path,
                        sample.creds_key,
                        self.link_creds,
                        verify=self.verify,
                    )
                )
        return verified_samples

    def register_new_creds(self, num_samples_added, samples):
        assert isinstance(num_samples_added, int)
        link_creds = self.link_creds
        creds_encoder = self.creds_encoder
        for i in range(num_samples_added):
            sample = samples[i]
            path, creds_key = get_path_creds_key(sample)
            encoded_creds_key = link_creds.get_encoding(creds_key, path)
            creds_encoder.register_samples((encoded_creds_key,), 1)
            if link_creds.add_to_used_creds(creds_key):
                save_link_creds(self.link_creds, self.cache)
                warn_missing_managed_creds(self.link_creds)

    def update_creds(self, sample_index: int, sample: Optional[LinkedSample]):
        link_creds = self.link_creds
        path, creds_key = get_path_creds_key(sample)
        encoded_creds_key = link_creds.get_encoding(creds_key, path)
        self.creds_encoder[sample_index] = (encoded_creds_key,)
        if link_creds.add_to_used_creds(creds_key):
            save_link_creds(self.link_creds, self.cache)
            warn_missing_managed_creds(self.link_creds)

    def read_shape_for_sample(self, global_sample_index: int) -> Tuple[int, ...]:
        sample = self.get_hub_read_sample(global_sample_index)
        if sample is None:
            return (0,)
        return sample.shape

    def read_sample_from_chunk(
        self,
        global_sample_index: int,
        chunk: BaseChunk,
        cast: bool = True,
        copy: bool = False,
        decompress: bool = True,
    ) -> np.ndarray:
        enc = self.chunk_id_encoder
        local_sample_index = enc.translate_index_relative_to_chunks(global_sample_index)
        sample_path = chunk.read_sample(
            local_sample_index, cast=cast, copy=copy, decompress=decompress
        )[0]

        creds_encoder = self.creds_encoder
        if not sample_path:
            return np.ones((0,))
        sample_creds_encoded = creds_encoder.get_encoded_creds_key(global_sample_index)
        sample_creds_key = self.link_creds.get_creds_key(sample_creds_encoded)
        return read_linked_sample(sample_path, sample_creds_key, self.link_creds, False)

    def check_link_ready(self):
        missing_keys = self.link_creds.missing_keys
        creds_used = self.link_creds.used_creds_keys
        missing_used_keys = {key for key in missing_keys if key in creds_used}
        if missing_used_keys:
            raise ValueError(
                f"Creds keys {missing_used_keys} are used in the data but not populated. Please populate the dataset using ds.populate_creds()."
            )
