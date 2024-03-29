import os
from glob import glob
import torch

def mkdir_ifnotexists(directory):
    if not os.path.exists(directory):
        os.mkdir(directory)

def get_class(kls):
    # import 一个类
    # ['datasets', 'scene_dataset', 'SceneDataset']
    parts = kls.split('.')
    # 'datasets.scene_dataset'
    module = ".".join(parts[:-1])
    # import datasets.scene_dataset.py
    m = __import__(module)
    # ['scene_dataset', 'SceneDataset']
    for comp in parts[1:]:
        m = getattr(m, comp)
    # datasets.scene_dataset.SceneDataset
    return m

def glob_numpy(path):
    files = []
    for ext in ['*.npy']:
        files.extend(glob(os.path.join(path, ext)))
    return files

def split_input(model_input, total_pixels):
    '''
     Split the input to fit Cuda memory for large resolution.
     Can decrease the value of n_pixels in case of cuda out of memory error.
     '''
    n_pixels = 10000
    split = []
    for i, indx in enumerate(torch.split(torch.arange(total_pixels).cuda(), n_pixels, dim=0)):
        data = model_input.copy()
        data['uv'] = torch.index_select(model_input['uv'], 1, indx)
        data['object_mask'] = torch.index_select(model_input['object_mask'], 1, indx)
        split.append(data)
    return split

def merge_output(res, total_pixels, batch_size):
    ''' Merge the split output. '''

    model_outputs = {}
    for entry in res[0]:
        if res[0][entry] is None:
            continue
        if len(res[0][entry].shape) == 1:
            model_outputs[entry] = torch.cat([r[entry].reshape(batch_size, -1, 1) for r in res],
                                             1).reshape(batch_size * total_pixels)
        else:
            model_outputs[entry] = torch.cat([r[entry].reshape(batch_size, -1, r[entry].shape[-1]) for r in res],
                                             1).reshape(batch_size * total_pixels, -1)

    return model_outputs