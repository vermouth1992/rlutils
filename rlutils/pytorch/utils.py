"""
Handle global pytorch device and data types
"""

import torch

device = None


def set_device(d):
    global device
    print(f'Setting global Pytorch device to {d}')
    if d == 'cuda':
        if not torch.cuda.is_available():
            print('CUDA is not available in this machine. Setting to cpu.')
            d = 'cpu'
    device = d


set_device('cuda')


def to_numpy(tensor):
    return tensor.detach().cpu().numpy()


cpu = torch.device('cpu')
cuda = []
for i in range(torch.cuda.device_count()):
    cuda.append(torch.device(f'cuda:{i}'))


def print_version():
    print(f'Pytorch version: {torch.__version__}, git version: {torch.version.git_version}')
