"""
Skeletonization algorithm based on TEASAR (Sato et al. 2000).

Authors: Alex Bae and Will Silversmith
Affiliation: Seung Lab, Princeton Neuroscience Institue
Date: June-August 2018
"""
from collections import defaultdict

import numpy as np
from scipy import ndimage
from PIL import Image

import igneous.dijkstra 
import igneous.skeletontricks

from .definitions import Skeleton, path2edge

from cloudvolume.lib import save_images, mkdir

def TEASAR(DBF, scale, const, max_boundary_distance=5000):
  """
  Given the euclidean distance transform of a label ("Distance to Boundary Function"), 
  convert it into a skeleton with scale and const TEASAR parameters. 

  DBF: Result of the euclidean distance transform. Must represent a single label.
  scale: during the "rolling ball" invalidation phase, multiply the DBF value by this.
  const: during the "rolling ball" invalidation phase, this is the minimum radius in voxels.
  max_boundary_distance: skip labels that have a DBF maximum value greater than this
    (e.g. for skipping somas). This value should be in nanometers, but if you are using
    this outside its original context it could be voxels.

  Based on the algorithm by:

  M. Sato, I. Bitter, M. Bender, A. Kaufman, and M. Nakajima. 
  "TEASAR: tree-structure extraction algorithm for accurate and robust skeletons"  
    Proc. the Eighth Pacific Conference on Computer Graphics and Applications. Oct. 2000.
    doi:10.1109/PCCGA.2000.883951 (https://ieeexplore.ieee.org/document/883951/)

  Returns: Skeleton object
  """
  labels = (DBF != 0).astype(np.bool)  
  any_voxel = igneous.skeletontricks.first_label(labels)   
  dbf_max = np.max(DBF)

  # > 5000 nm, gonna be a soma or blood vessel
  if any_voxel is None or dbf_max > max_boundary_distance: 
    return Skeleton()

  M = 1 / (dbf_max ** 1.01)

  # "4.4 DAF:  Compute distance from any voxel field"
  # Compute DAF, but we immediately convert to the PDRF
  # The extremal point of the PDRF is a valid root node
  # even if the DAF is computed from an arbitrary pixel.
  DBF[ DBF == 0 ] = np.inf
  DAF = igneous.dijkstra.distance_field(np.asfortranarray(labels), any_voxel)
  root = igneous.skeletontricks.find_target(labels, DAF)
  DAF = igneous.dijkstra.distance_field(np.asfortranarray(DBF), root)

  # save_images(DAF, directory="./saved_images/DAF")

  # Add p(v) to the DAF (pp. 4, section 4.5)
  # "4.5 PDRF: Compute penalized distance from root voxel field"
  # Let M > max(DBF)
  # p(v) = 5000 * (1 - DBF(v) / M)^16
  # 5000 is chosen to allow skeleton segments to be up to 3000 voxels
  # long without exceeding floating point precision.
  PDRF = DAF + (5000) * ((1 - (DBF * M)) ** 16) # 20x is a variation on TEASAR
  PDRF = PDRF.astype(np.float32)
  del DAF  

  paths = []
  valid_labels = np.count_nonzero(labels)
  
  while valid_labels > 0:
    target = igneous.skeletontricks.find_target(labels, PDRF)
    path = igneous.dijkstra.dijkstra(np.asfortranarray(PDRF), root, target)
    invalidated, labels = igneous.skeletontricks.roll_invalidation_ball(
      labels, DBF, path, scale, const 
    )
    valid_labels -= invalidated
    paths.append(path)

  skel_verts, skel_edges = path_union(paths)
  skel_radii = DBF[skel_verts[::3], skel_verts[1::3], skel_verts[2::3]]

  skel_verts = skel_verts.astype(np.float32).reshape( (skel_verts.size // 3, 3) )
  skel_edges = skel_edges.reshape( (skel_edges.size // 2, 2)  )

  return Skeleton(skel_verts, skel_edges, skel_radii)

def path_union(paths):
  """
  Given a set of paths with a common root, attempt to join them
  into a tree at the first common linkage.
  """
  tree = defaultdict(set)
  tree_id = {}
  vertices = []

  ct = 0
  for path in paths:
    for i in range(path.shape[0] - 1):
      parent = tuple(path[i, :].tolist())
      child = tuple(path[i + 1, :].tolist())
      tree[parent].add(child)
      if not parent in tree_id:
        tree_id[parent] = ct
        vertices.append(parent)
        ct += 1
      if not child in tree:
        tree[child] = set()
      if not child in tree_id:
        tree_id[child] = ct
        vertices.append(child)
        ct += 1 

  root = tuple(paths[0][0,:].tolist())
  edges = []

  def traverse(parent):
    for child in tree[parent]:
      edges.append([ tree_id[parent], tree_id[child] ])
      traverse(child)

  traverse(root)

  npv = np.zeros((len(vertices) * 3,), dtype=np.uint32)
  for i, vertex in enumerate(vertices):
    npv[ 3 * i + 0 ] = vertex[0]
    npv[ 3 * i + 1 ] = vertex[1]
    npv[ 3 * i + 2 ] = vertex[2]

  npe = np.zeros((len(edges) * 2,), dtype=np.uint32)
  for i, edge in enumerate(edges):
    npe[ 2 * i + 0 ] = edges[i][0]
    npe[ 2 * i + 1 ] = edges[i][1]

  return npv, npe

def xy_path_projection(paths, labels, N=0):
  if type(paths) != list:
    paths = [ paths ]

  projection = np.zeros( (labels.shape[0], labels.shape[1] ), dtype=np.uint8)
  outline = labels.any(axis=-1).astype(np.uint8) * 77
  outline = outline.reshape( (labels.shape[0], labels.shape[1] ) )
  projection += outline
  for path in paths:
    for coord in path:
      projection[coord[0], coord[1]] = 255

  projection = Image.fromarray(projection.T, 'L')
  N = str(N).zfill(3)
  mkdir('./saved_images/projections')
  projection.save('./saved_images/projections/{}.png'.format(N), 'PNG')

