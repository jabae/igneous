try:
    from StringIO import cStringIO as BytesIO
except ImportError:
    from io import BytesIO

import json
import pickle
import os
import re
from collections import defaultdict

import numpy as np
import scipy.ndimage
from tqdm import tqdm

from cloudvolume import CloudVolume
from cloudvolume.storage import Storage, SimpleStorage
from cloudvolume.lib import xyzrange, min2, max2, Vec, Bbox, mkdir, save_images
import edt # euclidean distance transform
from taskqueue import RegisteredTask

from igneous import chunks, downsample, downsample_scales
import fastremap

from .definitions import Skeleton
from .skeletonization import TEASAR
from .postprocess import (
  crop_skeleton, merge_skeletons, trim_skeleton,
  consolidate_skeleton
)


def skeldir(cloudpath):
  with SimpleStorage(cloudpath) as storage:
    info = storage.get_json('info')

  skel_dir = 'skeletons/'
  if 'skeletons' in info:
    skel_dir = info['skeletons']
  return skel_dir

class SkeletonTask(RegisteredTask):
  """
  Stage 1 of skeletonization.

  Convert chunks of segmentation into chunked skeletons and point clouds.
  They will be merged in the stage 2 task SkeletonMergeTask.
  """
  def __init__(self, cloudpath, shape, offset, mip, teasar_params, crop_zone, will_postprocess):
    super(SkeletonTask, self).__init__(cloudpath, shape, offset, mip, teasar_params, crop_zone, will_postprocess)
    self.cloudpath = cloudpath
    self.bounds = Bbox(offset, Vec(*shape) + Vec(*offset))
    self.mip = mip
    self.teasar_params = teasar_params
    self.crop_zone = crop_zone
    self.will_postprocess = will_postprocess

  def execute(self):
    vol = CloudVolume(self.cloudpath, mip=self.mip, cache=True)
    bbox = Bbox.clamp(self.bounds, vol.bounds)

    all_labels = vol[ bbox.to_slices() ]
    all_labels = all_labels[:,:,:,0]
    all_labels, remap = fastremap.renumber(all_labels)
    iremap = { v:k for k,v in remap.items() }

    path = skeldir(self.cloudpath)
    path = os.path.join(self.cloudpath, path)

    all_dbf = edt.edt(all_labels, anisotropy=vol.resolution.tolist())
    print("EDT Done.")

    # for segid in np.unique(all_labels):
    for segid in (remap[28823174],):
      self.process_label(segid, iremap, all_labels, all_dbf, bbox)

  def process_label(self, segid, remap, all_labels, all_dbf, bbox):
    if remap[segid] == 0:
      return

    print(remap[segid])
    skeletons = []
    for dbf, roi in self.components(segid, all_labels, all_dbf, bbox):
      print(roi)
      # save_images(dbf)
      skeleton = self.skeletonize(dbf, roi)
      if not skeleton.empty():
        skeletons.append(skeleton)
      break

    num_skels = len(skeletons)

    print(num_skels)

    if num_skels == 0:
      print("no skels")
      return

    skeleton = skeletons[0]
    num_vertices = skeleton.nodes.shape[0]
    for i in range(1, num_skels):
      skeleton.nodes = np.concatenate(skeleton.nodes, skeletons[i].nodes)
      edges = skeletons[i].edges + num_vertices
      skeleton.edges = np.concatenate(skeleton.edges, edges)
      skeleton.radii = np.concatenate(skeleton.radii, skeletons[i].radii)
    
    # if self.will_postprocess:
    #   stor.put_file(
    #     file_path="{}:skel:{}".format(segid, bbox.to_filename()),
    #     content=pickle.dumps(skeleton),
    #     compress='gzip',
    #     content_type="application/python-pickle",
    #   )
    # else:
    skeleton.nodes[:] *= vol.resolution
    print(remap[segid])
    print(skeleton)
    vol.skeleton.upload(remap[segid], skeleton.nodes, skeleton.edges, skeleton.radii)    

  def components(self, segid, all_labels, all_dbf, bbox):
    labels = (all_labels == segid)
    labels, N = scipy.ndimage.measurements.label(labels)
    labels = labels.astype(np.uint8)
    labels = np.copy(labels, order='F')

    for i in range(1, N):
      component = (labels == i)

      save_images(component, directory='./saved_images/component/')

      slices = scipy.ndimage.find_objects(component)[0]
      roi = Bbox.from_slices(slices)

      if roi.volume() <= 1:
        continue

      dbf = component[slices] * all_dbf[slices]
      save_images(component[slices])
      save_images(all_dbf[slices], directory='./saved_images/alldbf/')
      del component

      roi += bbox.minpt 
      
      yield dbf, roi

  def skeletonize(self, dbf, bbox):
    skeleton = TEASAR(dbf, self.teasar_params)

    skeleton.nodes[:,0] += bbox.minpt.x
    skeleton.nodes[:,1] += bbox.minpt.y
    skeleton.nodes[:,2] += bbox.minpt.z

    # Crop by 50px to avoid edge effects.
    crop_bbox = bbox.clone()
    crop_bbox.minpt += self.crop_zone
    crop_bbox.maxpt -= self.crop_zone

    if crop_bbox.volume() <= 0:
      return skeleton

    return crop_skeleton(skeleton, crop_bbox)

class SkeletonMergeTask(RegisteredTask):
  """
  Stage 2 of skeletonization.

  Combine point cloud chunks into a single unified point cloud.

  If we parallelize using prefixes single digit prefixes ['0','1',..'9'] all meshes will
  be correctly processed. But if we do ['10','11',..'99'] meshes from [0,9] won't get
  processed and need to be handle specifically by creating tasks that will process
  a single mesh ['0:','1:',..'9:']
  """
  def __init__(self, cloudpath, prefix):
    super(SkeletonMergeTask, self).__init__(cloudpath, prefix)
    self.cloudpath = cloudpath
    self.prefix = prefix

  def execute(self):
    self.vol = CloudVolume(self.cloudpath)

    with Storage(self.cloudpath) as storage:
      self.agglomerate(storage)

  def get_filenames_subset(self, storage):
    prefix = '{}/{}'.format(self.vol.skeleton.path, self.prefix)
    skeletons = defaultdict(list)

    for filename in storage.list_files(prefix=prefix):
      # `match` implies the beginning (^). `search` matches whole string
      matches = re.search(r'(\d+):skel:', filename)

      if not matches:
        continue

      segid, = matches.groups()
      segid = int(segid)
      skeletons[segid].append(filename)

    return skeletons

  def agglomerate(self, stor):
    skels = self.get_filenames_subset(stor)

    vol = self.vol

    for segid, frags in tqdm(skels.items()):
      ptcloud = self.get_point_cloud(vol, segid, frags)    
      skeleton = self.fuse_skeletons(frags, stor)
      skeleton = trim_skeleton(skeleton, ptcloud)

      vol.skeleton.upload(segid, skeleton.nodes, skeleton.edges, skeleton.radii)

      # Used for recomputing point clouds
      stor.put_json(
        file_path="{}/{}.json".format(vol.skeleton.path, segid),
        content={ "fragments": [ os.path.basename(fname) for fname in frags ] },
        cache_control="no-cache",
      )

    stor.wait()

    for segid, frags in skels.items():
      stor.delete_files(frags)

  def get_point_cloud(self, vol, segid, frags):
    ptcloud = np.array([], dtype=np.uint16).reshape(0, 3)
    for frag in frags:
      bbox = Bbox.from_filename(frag)
      img = vol[ bbox.to_slices() ][:,:,:,0]
      ptc = np.argwhere( img == segid )
      ptcloud = np.concatenate((ptcloud, ptc), axis=0)

    if ptcloud.size == 0:
      return ptcloud

    ptcloud.sort(axis=0) # sorts x column, but not y unfortunately
    return np.unique(ptcloud, axis=0)

  def fuse_skeletons(self, filenames, storage):
    if len(filenames) == 0:
      return Skeleton()
    
    skldl = storage.get_files(filenames)
    skeletons = { item['filename'] : pickle.loads(item['content']) for item in skldl }

    if len(skeletons) == 1:
      return skeletons[filenames[0]]

    file_pairs = self.find_paired_skeletons(filenames)

    for fname1, fname2 in file_pairs:
      skel1, skel2 = skeletons[fname1], skeletons[fname2]
      skel1, skel2 = merge_skeletons(skel1, skel2)
      skeletons[fname1] = skel1
      skeletons[fname2] = skel2

    skeletons = list(skeletons.values())

    fusing = skeletons[0]
    offset = 0
    for skel in skeletons[1:]:
      if skel.edges.shape[0] == 0:                                                                                                                                                                                                                                                                                                                                                            
        continue

      skel.edges = skel.edges.astype(np.uint32)
      skel.edges += offset
      offset += skel.nodes.shape[0]

      fusing.nodes = np.concatenate((fusing.nodes, skel.nodes), axis=0)
      fusing.edges = np.concatenate((fusing.edges, skel.edges), axis=0)
      fusing.radii = np.concatenate((fusing.radii, skel.radii), axis=0)

    return consolidate_skeleton(fusing)

  def find_paired_skeletons(self, filenames):
    pairs = []

    for i in range(len(filenames) - 1):
      adj1 = Bbox.from_filename(filenames[i])
      for j in range(i + 1, len(filenames)):
        adj2 = Bbox.from_filename(filenames[j])

        # We're testing for overlap, tasks
        # are created with 50% overlap
        if Bbox.intersects(adj1, adj2):
          pairs.append(
            (filenames[i], filenames[j])
          )

    return pairs







