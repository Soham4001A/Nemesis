import torch
from torch.utils.data import Dataset
import tensorflow as tf
import numpy as np
import trimesh
import json
import os
import itertools # Import itertools
from src.data_processing.mesh_data_extractor import sample_points_from_mesh, normalize_point_cloud, generate_sdf

# The following functions are adapted from the MeshGraphNets repository
# https://github.com/deepmind/deepmind-research/blob/master/meshgraphnets/common.py

def _parse_function(example_proto, meta):
    feature_lists = {k: tf.io.VarLenFeature(tf.string)
                   for k in meta['field_names']}
    features = tf.io.parse_single_example(example_proto, feature_lists)
    out = {}
    for key, field in meta['features'].items():
        data = tf.io.decode_raw(features[key].values, getattr(tf, field['dtype']))
        data = tf.reshape(data, field['shape'])
        if field['type'] == 'static':
            data = tf.tile(data, [meta['trajectory_length'], 1, 1])
        elif field['type'] == 'dynamic_varlen':
            length = tf.io.decode_raw(features['length_'+key].values, tf.int32)
            length = tf.reshape(length, [-1])
            data = tf.RaggedTensor.from_row_lengths(data, row_lengths=length)
        elif field['type'] != 'dynamic':
            raise ValueError('invalid data format')
        out[key] = data
    return out

def get_tfrecord_iterator(tfrecord_path):
    with open(os.path.join(os.path.dirname(tfrecord_path), 'meta.json'), 'r') as fp:
        meta = json.loads(fp.read())

    dataset = tf.data.TFRecordDataset(tfrecord_path)
    dataset = dataset.map(lambda x: _parse_function(x, meta))
    return dataset.as_numpy_iterator()

def get_target_property(record):
    # For airfoil, use the average pressure at the last time step
    # pressure is (trajectory_length, num_nodes, 1)
    return np.mean(record['pressure'][-1])

class MeshDataset(Dataset):
    def __init__(self, tfrecord_path, num_points=2048, num_sdf_points=1024, is_local=False, batch_size=1):
        self.tfrecord_path = tfrecord_path
        self.num_points = num_points
        self.num_sdf_points = num_sdf_points
        
        # Load a limited number of samples for local mode, otherwise load all
        if is_local:
            # Load only a few batches for quick local testing
            self.dataset = list(itertools.islice(get_tfrecord_iterator(tfrecord_path), batch_size * 2)) # Load 2 batches worth
        else:
            # Load the entire dataset into memory (for full training)
            self.dataset = list(get_tfrecord_iterator(tfrecord_path))
        self.length = len(self.dataset)

    def __len__(self):
        return self.length

    def __getitem__(self, idx):
        record = self.dataset[idx]

        if 'mesh_pos' in record:
            positions = record['mesh_pos'][-1]
        elif 'world_pos' in record:
            positions = record['world_pos'][-1]
        else:
            raise ValueError("Positions not found in record")
        cells = record['cells'][-1]

        # Ensure positions are 3D for trimesh
        if positions.shape[1] == 2:
            positions_for_trimesh = np.hstack([positions, np.zeros((positions.shape[0], 1))])
        else:
            positions_for_trimesh = positions

        mesh = trimesh.Trimesh(vertices=positions_for_trimesh, faces=cells)

        # Sample points from the mesh surface
        sampled_points, sampled_normals = sample_points_from_mesh(positions_for_trimesh, cells, self.num_points)
        normalized_points = normalize_point_cloud(sampled_points)

        # Sample points for SDF calculation
        query_points = np.random.rand(self.num_sdf_points, 3) * 2 - 1 # Points in [-1, 1] cube
        sdf_values = trimesh.proximity.signed_distance(mesh, query_points)

        # Get the label
        label = get_target_property(record)

        return {
            'points': torch.from_numpy(normalized_points.copy()).float(),
            'normals': torch.from_numpy(sampled_normals.copy()).float(),
            'cells': torch.from_numpy(cells.copy()).long(), # Add cells to the output
            'sdf_points': torch.from_numpy(query_points.copy()).float(),
            'sdf_values': torch.from_numpy(sdf_values.copy()).float(),
            'label': torch.from_numpy(np.array(label).copy()).float(),
        }

def collate_fn(batch):
    return {
        'points': torch.stack([item['points'] for item in batch]),
        'normals': torch.stack([item['normals'] for item in batch]),
        'cells': torch.stack([item['cells'] for item in batch]), # Add cells to the collated batch
        'sdf_points': torch.stack([item['sdf_points'] for item in batch]),
        'sdf_values': torch.stack([item['sdf_values'] for item in batch]),
        'label': torch.stack([item['label'] for item in batch]),
    }