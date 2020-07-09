import gzip
import os
import tarfile
import tempfile
import zipfile

import numpy as np
import torch
import torchvision.transforms as transforms
from PIL import Image
from fcdd.datasets.bases import GTMapADDataset
from fcdd.util import imsave
from torchvision.datasets import VisionDataset
from torchvision.datasets.imagenet import check_integrity, verify_str_arg
from torchvision.datasets.utils import download_url, _is_gzip, _is_tar, _is_targz, _is_zip


class MvTec(VisionDataset, GTMapADDataset):
    url = "ftp://guest:GU%2E205dldo@ftp.softronics.ch/mvtec_anomaly_detection/mvtec_anomaly_detection.tar.xz"
    base_folder = 'mvtec'
    labels = (
        'bottle', 'cable', 'capsule', 'carpet', 'grid', 'hazelnut', 'leather',
        'metal_nut', 'pill', 'screw', 'tile', 'toothbrush', 'transistor',
        'wood', 'zipper'
    )
    normal_anomaly_label = 'good'
    normal_anomaly_label_idx = 0

    def __init__(self, root, split='train', target_transform=None, img_gt_transform=None,
                 transform=None, all_transform=None, download=True, shape=(3, 300, 300), enlarge=False,
                 normal_classes=(), nominal_label=0, anomalous_label=1, logger=None
                 ):
        super(MvTec, self).__init__(root, transform=transform, target_transform=target_transform)
        self.split = verify_str_arg(split, "split", ("train", "test", "test_anomaly_label_target"))
        self.img_gt_transform = img_gt_transform
        self.all_transform = all_transform
        self.shape = shape
        self.enlarge = enlarge
        self.orig_gtmaps = None
        self.normal_classes = normal_classes
        self.nominal_label = nominal_label
        self.anom_label = anomalous_label
        self.logger = logger

        if download:
            self.download(shape=self.shape[1:])

        print('Loading dataset from {}...'.format(self.data_file))
        dataset_dict = torch.load(self.data_file)
        self.anomaly_label_strings = dataset_dict['anomaly_label_strings']
        if self.split == 'train':
            self.data, self.targets = dataset_dict['train_data'], dataset_dict['train_labels']
            self.gt, self.anomaly_labels = None, None
        else:
            self.data, self.targets = dataset_dict['test_data'], dataset_dict['test_labels']
            self.gt, self.anomaly_labels = dataset_dict['test_maps'], dataset_dict['test_anomaly_labels']

        if self.enlarge:
            self.data, self.targets = self.data.repeat(10, 1, 1, 1), self.targets.repeat(10)
            self.gt = self.gt.repeat(10, 1, 1) if self.gt is not None else None
            self.anomaly_labels = self.anomaly_labels.repeat(10) if self.anomaly_labels is not None else None
            self.orig_gtmaps = self.orig_gtmaps.repeat(10, 1, 1) if self.orig_gtmaps is not None else None

        if self.nominal_label != 0:
            print('Swapping labels, i.e. anomalies are 0 and nominals are 1, same for GT maps.')
            assert -3 not in [self.nominal_label, self.anom_label]
        print('Dataset complete.')

    def __getitem__(self, index):
        img, label = self.data[index], self.targets[index]

        if self.split == 'test_anomaly_label_target':
            label = self.target_transform(self.anomaly_labels[index])
        if self.target_transform is not None:
            label = self.target_transform(label)

        if self.split == 'train' and self.gt is None:
            assert self.anom_label in [0, 1]
            # gt is assumed to be 1 for anoms always (regardless of the anom_label), since the supervisers work that way
            # later code fixes that (and thus would corrupt it if the correct anom_label is used here in swapped case)
            gtinitlbl = label if self.anom_label == 1 else (1 - label)
            gt = (torch.ones_like(img)[0] * gtinitlbl).mul(255).byte()
        else:
            gt = self.gt[index]

        if self.all_transform is not None:
            img, gt, label = self.all_transform((img, gt, label))
            gt = gt.mul(255).byte() if gt.dtype != torch.uint8 else gt
            img = img.sub(img.min()).div(img.max() - img.min()).mul(255).byte() if img.dtype != torch.uint8 else img

        # doing this so that it is consistent with all other datasets
        # to return a PIL Image
        img = Image.fromarray(img.transpose(0, 2).transpose(0, 1).numpy(), mode='RGB')
        gt = Image.fromarray(gt.squeeze(0).numpy(), mode='L')

        if self.img_gt_transform is not None:
            img, gt = self.img_gt_transform((img, gt))

        if self.transform is not None:
            img = self.transform(img)

        if self.nominal_label != 0:
            gt[gt == 0] = -3  # -3 is chosen arbitrarily here
            gt[gt == 1] = self.anom_label
            gt[gt == -3] = self.nominal_label

        return img, label, gt

    def __len__(self):
        return len(self.data)

    def download(self, verbose=True, shape=None, cls=None):
        assert shape is not None or cls is not None, 'original shape requires a class'
        if not check_integrity(self.data_file if shape is not None else self.orig_data_file(cls)):
            tmp_dir = tempfile.mkdtemp()
            self.download_and_extract_archive(
                self.url, os.path.join(self.root, self.base_folder), extract_root=tmp_dir,
            )
            train_data, train_labels = [], []
            test_data, test_labels, test_maps, test_anomaly_labels = [], [], [], []
            anomaly_labels, albl_idmap = [], {self.normal_anomaly_label: self.normal_anomaly_label_idx}

            for lbl_idx, lbl in enumerate(self.labels if cls is None else [self.labels[cls]]):
                if verbose:
                    print('Processing data for label {}...'.format(lbl))
                for anomaly_label in sorted(os.listdir(os.path.join(tmp_dir, lbl, 'test'))):
                    for img_name in sorted(os.listdir(os.path.join(tmp_dir, lbl, 'test', anomaly_label))):
                        with open(os.path.join(tmp_dir, lbl, 'test', anomaly_label, img_name), 'rb') as f:
                            sample = Image.open(f)
                            sample = self.img_to_torch(sample, shape)
                        if anomaly_label != self.normal_anomaly_label:
                            mask_name = self.convert_img_name_to_mask_name(img_name)
                            with open(os.path.join(tmp_dir, lbl, 'ground_truth', anomaly_label, mask_name), 'rb') as f:
                                mask = Image.open(f)
                                mask = self.img_to_torch(mask, shape)
                        else:
                            mask = torch.zeros_like(sample)
                        test_data.append(sample)
                        test_labels.append(cls if cls is not None else lbl_idx)
                        test_maps.append(mask)
                        if anomaly_label not in albl_idmap:
                            albl_idmap[anomaly_label] = len(albl_idmap)
                        test_anomaly_labels.append(albl_idmap[anomaly_label])

                for anomaly_label in sorted(os.listdir(os.path.join(tmp_dir, lbl, 'train'))):
                    for img_name in sorted(os.listdir(os.path.join(tmp_dir, lbl, 'train', anomaly_label))):
                        with open(os.path.join(tmp_dir, lbl, 'train', anomaly_label, img_name), 'rb') as f:
                            sample = Image.open(f)
                            sample = self.img_to_torch(sample, shape)
                        train_data.append(sample)
                        train_labels.append(lbl_idx)

            anomaly_labels = list(zip(*sorted(albl_idmap.items(), key=lambda kv: kv[1])))[0]
            train_data = torch.stack(train_data)
            train_labels = torch.IntTensor(train_labels)
            test_data = torch.stack(test_data)
            test_labels = torch.IntTensor(test_labels)
            test_maps = torch.stack(test_maps)[:, 0, :, :]  # r=g=b -> grayscale
            test_anomaly_labels = torch.IntTensor(test_anomaly_labels)
            torch.save(
                {'train_data': train_data, 'train_labels': train_labels,
                 'test_data': test_data, 'test_labels': test_labels,
                 'test_maps': test_maps, 'test_anomaly_labels': test_anomaly_labels,
                 'anomaly_label_strings': anomaly_labels},
                self.data_file if shape is not None else self.orig_data_file(cls)
            )

        else:
            print('Files already downloaded.')
            return

    def get_original_gtmaps_normal_class(self):
        assert self.split != 'train', 'original maps are only available for test mode'
        assert len(self.normal_classes) == 1, 'normal classes must be known and there must be exactly one'
        assert self.all_transform is None, 'all_transform would be skipped here'
        assert all([isinstance(t, (transforms.Resize, transforms.ToTensor)) for t in self.img_gt_transform.transforms])
        if self.orig_gtmaps is None:
            self.download(shape=None, cls=self.normal_classes[0])
            orig_ds = torch.load(self.orig_data_file(self.normal_classes[0]))
            self.orig_gtmaps = orig_ds['test_maps'].unsqueeze(1).div(255)
        return self.orig_gtmaps

    def print(self, path, size=10, separate=False, classes=range(15)):
        pics = []
        for c in classes:
            alsize = len(set(self.anomaly_labels[self.targets == c].tolist()))
            counter = 0
            for al in sorted(set(self.anomaly_labels[self.targets == c].tolist())):
                if counter >= size:
                    print(
                        'WARNING: For class {} there are more anomaly labels '
                        '({}) than size ({}) fits, thus some are skipped.'
                        .format(c, alsize, size)
                    )
                    break
                n = max(size // alsize, 1)
                if al == 0 and size // alsize > 0:
                    n = size // alsize + size % alsize
                img = self.data[(self.targets == c) * (self.anomaly_labels == al)][:n]
                pics.append(img)
                counter += n
            counter = 0
            for al in sorted(set(self.anomaly_labels[self.targets == c].tolist())):
                if counter >= size:
                    break
                n = max(size // alsize, 1)
                if al == 0 and size // alsize > 0:
                    n = size // alsize + size % alsize
                img = self.gt[(self.targets == c) * (self.anomaly_labels == al)][:n].unsqueeze(1).repeat(1, 3, 1, 1)
                pics.append(img)
                counter += n
            if separate:
                pics = torch.cat(pics)
                imsave(pics, path.replace('.', '_{}.'.format(c)), size, norm=True)
                pics = []
        if not separate:
            pics = torch.cat(pics)
            imsave(pics, path, size, norm=True)

    @property
    def data_file(self):
        return os.path.join(self.root, self.base_folder, self.filename)

    @property
    def filename(self):
        return "admvtec_{}x{}.pt".format(self.shape[1], self.shape[2])

    def orig_data_file(self, cls):
        return os.path.join(self.root, self.base_folder, self.orig_filename(cls))

    def orig_filename(self, cls):
        return "admvtec_orig_cls{}.pt".format(cls)

    @staticmethod
    def img_to_torch(img, shape=None):
        if shape is not None:
            return torch.nn.functional.interpolate(
                torch.from_numpy(np.array(img.convert('RGB'))).float().transpose(0, 2).transpose(1, 2)[None, :],
                shape
            )[0].byte()
        else:
            return torch.from_numpy(
                np.array(img.convert('RGB'))
            ).float().transpose(0, 2).transpose(1, 2)[None, :][0].byte()

    @staticmethod
    def convert_img_name_to_mask_name(img_name):
        return img_name.replace('.png', '_mask.png')

    @staticmethod
    def download_and_extract_archive(url, download_root, extract_root=None, filename=None,
                                     md5=None, remove_finished=False):
        download_root = os.path.expanduser(download_root)
        if extract_root is None:
            extract_root = download_root
        if not filename:
            filename = os.path.basename(url)
        if not os.path.exists(download_root):
            os.makedirs(download_root)
        if not check_integrity(os.path.join(download_root, filename)):
            download_url(url, download_root, filename, md5)

        archive = os.path.join(download_root, filename)
        print("Extracting {} to {}".format(archive, extract_root))
        MvTec.extract_archive(archive, extract_root, remove_finished)

    @staticmethod
    def extract_archive(from_path, to_path=None, remove_finished=False):
        if to_path is None:
            to_path = os.path.dirname(from_path)

        if _is_tar(from_path):
            with tarfile.open(from_path, 'r') as tar:
                tar.extractall(path=to_path)
        elif _is_targz(from_path):
            with tarfile.open(from_path, 'r:gz') as tar:
                tar.extractall(path=to_path)
        elif _is_gzip(from_path):
            to_path = os.path.join(to_path, os.path.splitext(os.path.basename(from_path))[0])
            with open(to_path, "wb") as out_f, gzip.GzipFile(from_path) as zip_f:
                out_f.write(zip_f.read())
        elif _is_zip(from_path):
            with zipfile.ZipFile(from_path, 'r') as z:
                z.extractall(to_path)
        elif MvTec._is_tarxz(from_path):
            with tarfile.open(from_path, 'r:xz') as tar:
                tar.extractall(path=to_path)
        else:
            raise ValueError("Extraction of {} not supported".format(from_path))

    @staticmethod
    def _is_tarxz(filename):
        return filename.endswith(".tar.xz")