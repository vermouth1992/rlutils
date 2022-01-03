from typing import Dict

import gym
import numpy as np
import torch

import rlutils.pytorch.utils as ptu
from rlutils.replay_buffers.utils import combined_shape
from .base import Storage


class PyDictStorage(Storage):
    def __init__(self, data_spec: Dict[str, gym.spaces.Space], capacity):
        self.data_spec = data_spec
        self.max_size = capacity
        self.storage = self._create_storage()

    def _create_storage(self):
        return {key: np.zeros(combined_shape(self.capacity, item.shape), dtype=item.dtype)
                for key, item in self.data_spec.items()}

    def reset(self):
        self.ptr = 0
        self.size = 0

    def __len__(self):
        return self.size

    def __getitem__(self, item):
        return {key: data[item] for key, data in self.storage.items()}

    @property
    def capacity(self):
        return self.max_size

    def get_available_indexes(self, batch_size):
        if self.ptr + batch_size > self.max_size:
            index = np.concatenate((np.arange(self.ptr, self.capacity),
                                    np.arange(batch_size - (self.capacity - self.ptr))), axis=0)
            print('Reaches the end of the replay buffer')
        else:
            index = np.arange(self.ptr, self.ptr + batch_size)
        return index

    def add(self, data: Dict[str, np.ndarray]):
        batch_size = list(data.values())[0].shape[0]
        index = self.get_available_indexes(batch_size)
        for key, item in data.items():
            assert batch_size == item.shape[0], 'The batch size in the data is not consistent'
            self.storage[key][index] = item
        self.ptr = (self.ptr + batch_size) % self.capacity
        self.size = min(self.size + batch_size, self.capacity)
        return index

    def get(self):
        return self.storage


class MemoryEfficientPyDictStorage(PyDictStorage):
    def _create_storage(self):
        storage = {}
        self.np_key = []
        self.obj_key = []
        for key, item in self.data_spec.items():
            if isinstance(item, gym.spaces.Space):
                storage[key] = np.zeros(combined_shape(self.capacity, item.shape), dtype=item.dtype)
                self.np_key.append(key)
            else:
                print(f"Store key {key} as an object")
                storage[key] = np.zeros(self.capacity, dtype=object)
                self.obj_key.append(key)
        return storage

    def __getitem__(self, item):
        data = {key: self.storage[key][item] for key in self.np_key}
        for key in self.obj_key:
            output = []
            for idx in item:
                output.append(self.storage[key][idx])
            data[key] = np.array(output)
        return data

    def add(self, data: Dict[str, np.ndarray]):
        batch_size = data[self.np_key[0]].shape[0]
        index = self.get_available_indexes(batch_size)
        for key, item in data.items():
            if isinstance(item, np.ndarray):
                self.storage[key][index] = item
            elif isinstance(item, list):
                for i in range(batch_size):
                    self.storage[key][(self.ptr + i) % self.max_size] = item[i]
            else:
                raise ValueError(f'Unknown type {type(item)}')

        self.ptr = (self.ptr + batch_size) % self.capacity
        self.size = min(self.size + batch_size, self.capacity)
        return index


"""
Implemented but reserved for future use
"""


class TorchDictStorage(PyDictStorage):
    def __init__(self, device=ptu.device, **kwargs):
        self.device = device
        super(TorchDictStorage, self).__init__(**kwargs)

    def _create_storage(self):
        storage = super(TorchDictStorage, self)._create_storage()
        for key, item in storage.items():
            storage[key] = torch.as_tensor(item, device=self.device)
        return storage

    def get_available_indexes(self, batch_size):
        if self.ptr + batch_size > self.max_size:
            index = torch.cat((torch.arange(self.ptr, self.capacity, device=self.device),
                               torch.arange(batch_size - (self.capacity - self.ptr), device=self.device)), dim=0)
            print('Reaches the end of the replay buffer')
        else:
            index = torch.arange(self.ptr, self.ptr + batch_size, device=self.device)
        return index