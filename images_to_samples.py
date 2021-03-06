import argparse
import datetime
import os
import fiona
import numpy as np
import warnings
import rasterio
import time

from pathlib import Path
from tqdm import tqdm
from collections import OrderedDict

from utils.CreateDataset import create_files_and_datasets, MetaSegmentationDataset
from utils.utils import vector_to_raster, get_key_def, lst_ids
from utils.readers import read_parameters, image_reader_as_array, read_csv
from utils.verifications import is_valid_geom, validate_num_classes

# from rasterio.features import is_valid_geom #FIXME: wait for https://github.com/mapbox/rasterio/issues/1815 to be solved

try:
    import boto3
except ModuleNotFoundError:
    warnings.warn("The boto3 library couldn't be imported. Ignore if not using AWS s3 buckets", ImportWarning)
    pass


def mask_image(arrayA, arrayB):
    """Function to mask values of arrayB, based on 0 values from arrayA.

    >>> x1 = np.array([0, 2, 4, 6, 0, 3, 9, 8], dtype=np.uint8).reshape(2,2,2)
    >>> x2 = np.array([1.5, 1.2, 1.6, 1.2, 11., 1.1, 25.9, 0.1], dtype=np.float32).reshape(2,2,2)
    >>> mask_image(x1, x2)
    array([[[ 0. ,  0. ],
        [ 1.6,  1.2]],
        [[11. ,  1.1],
        [25.9,  0.1]]], dtype=float32)
    """

    # Handle arrayA of shapes (h,w,c) and (h,w)
    if len(arrayA.shape) == 3:
        mask = arrayA[:, :, 0] != 0
    else:
        mask = arrayA != 0

    ma_array = np.zeros(arrayB.shape, dtype=arrayB.dtype)
    # Handle arrayB of shapes (h,w,c) and (h,w)
    if len(arrayB.shape) == 3:
        for i in range(0, arrayB.shape[2]):
            ma_array[:, :, i] = mask * arrayB[:, :, i]
    else:
        ma_array = arrayB * mask
    return ma_array


def pad_diff(arr, w, h, arr_shape):
    """ Pads img_arr width or height < samples_size with zeros """
    w_diff = arr_shape - w
    h_diff = arr_shape - h

    if len(arr.shape) > 2:
        padded_arr = np.pad(arr, ((0, w_diff), (0, h_diff), (0, 0)), "constant", constant_values=(0, 0))
    else:
        padded_arr = np.pad(arr, ((0, w_diff), (0, h_diff)), "constant", constant_values=(0, 0))

    return padded_arr


def append_to_dataset(dataset, sample):
    old_size = dataset.shape[0]  # this function always appends samples on the first axis
    dataset.resize(old_size + 1, axis=0)
    dataset[old_size, ...] = sample
    return old_size  # the index to the newly added sample, or the previous size of the dataset


def check_sampling_dict():
    for i, (key, value) in enumerate(params['sample']['sampling'].items()):

        if i == 0:
            if key == 'method':
                for j in range(len(value)):
                    if value[j] == 'min_annotated_percent' or value[j] == 'class_proportion':
                        pass
                    else:
                        raise ValueError(f"Method value must be min_annotated_percent or class_proportion."
                                         f" Provided value is {value[j]}")
            else:
                raise ValueError(f"Ordereddict first key value must be method. Provided value is {key}")
        elif i == 1:
            if key == 'map':
                if type(value) == int:
                    pass
                else:
                    raise ValueError(f"Value type must be 'int'. Provided value is {type(value)}")
            else:
                raise ValueError(f"Ordereddict second key value must be map. Provided value is {key}")
        elif i >= 2:
            if type(int(key)) == int:
                pass


def minimum_annotated_percent(target_background_percent, min_annotated_percent):
    if float(target_background_percent) <= 100 - min_annotated_percent:
        return True

    return False


def class_proportion(target):
    prop_classes = {}
    sample_total = (params['global']['samples_size']) ** 2
    for i in range(0, params['global']['num_classes'] + 1):
        prop_classes.update({str(i): 0})
        if i in np.unique(target.flatten()):
            prop_classes[str(i)] = (round((np.bincount(target.flatten())[i] / sample_total) * 100, 1))

    condition = []
    for i, (key, value) in enumerate(params['sample']['sampling'].items()):
        if i >= 2 and prop_classes[key] >= value:
            condition.append(1)

    if sum(condition) == (params['global']['num_classes'] + 1):
        return True

    return False


def compute_classes(dataset, samples_file, val_percent, val_sample_file, data, target, metadata_idx, dict_classes):
    """ Creates Dataset (trn, val, tst) appended to Hdf5 and computes pixel classes(%) """
    val = False
    if dataset == 'trn':
        random_val = np.random.randint(1, 100)
        if random_val > val_percent:
            pass
        else:
            val = True
            samples_file = val_sample_file
    append_to_dataset(samples_file["sat_img"], data)
    append_to_dataset(samples_file["map_img"], target)
    append_to_dataset(samples_file["meta_idx"], metadata_idx)

    # adds pixel count to pixel_classes dict for each class in the image
    for i in (np.unique(target)):
        dict_classes[i] += (np.bincount(target.flatten()))[i]

    return val


def samples_preparation(in_img_array,
                        label_array,
                        sample_size,
                        overlap,
                        samples_count,
                        num_classes,
                        samples_file,
                        val_percent,
                        val_sample_file,
                        dataset,
                        pixel_classes,
                        image_metadata=None):
    """
    Extract and write samples from input image and reference image
    :param in_img_array: numpy array of the input image
    :param label_array: numpy array of the annotation image
    :param sample_size: (int) Size (in pixel) of the samples to create #FIXME: could there be a different sample size for tst dataset? shows results closer to inference
    :param overlap: (int) Desired overlap between samples in %
    :param samples_count: (dict) Current number of samples created (will be appended and return)
    :param num_classes: (dict) Number of classes in reference data (will be appended and return)
    :param samples_file: (hdf5 dataset) hdfs file where samples will be written
    :param val_percent: (int) percentage of validation samples
    :param val_sample_file: (hdf5 dataset) hdfs file where samples will be written (val)
    :param dataset: (str) Type of dataset where the samples will be written. Can be 'trn' or 'val' or 'tst'
    :param pixel_classes: (dict) samples pixel statistics
    :param image_metadata: (Ruamel) list of optionnal metadata specified in the associated metadata file
    :return: updated samples count and number of classes.
    """

    # read input and reference images as array

    h, w, num_bands = in_img_array.shape
    if dataset == 'trn':
        idx_samples = samples_count['trn']
    elif dataset == 'tst':
        idx_samples = samples_count['tst']
    else:
        raise ValueError(f"Dataset value must be trn or val. Provided value is {dataset}")

    metadata_idx = -1
    idx_samples_v = samples_count['val']
    if image_metadata:
        # there should be one set of metadata per raster
        # ...all samples created by tiling below will point to that metadata by index
        metadata_idx = append_to_dataset(samples_file["metadata"], repr(image_metadata))

    dist_samples = round(sample_size * (1 - (overlap / 100)))
    added_samples = 0
    excl_samples = 0

    with tqdm(range(0, h, dist_samples), position=1, leave=True,
              desc=f'Writing samples to "{dataset}" dataset. Dataset currently contains {idx_samples} '
                   f'samples.') as _tqdm:

        for row in _tqdm:
            for column in range(0, w, dist_samples):
                data = (in_img_array[row:row + sample_size, column:column + sample_size, :])
                target = np.squeeze(label_array[row:row + sample_size, column:column + sample_size, :], axis=2)
                data_row = data.shape[0]
                data_col = data.shape[1]
                if data_row < sample_size or data_col < sample_size:
                    data = pad_diff(data, data_row, data_col, sample_size)

                target_row = target.shape[0]
                target_col = target.shape[1]
                if target_row < sample_size or target_col < sample_size:
                    target = pad_diff(target, target_row, target_col, sample_size)
                u, count = np.unique(target, return_counts=True)
                target_background_percent = round(count[0] / np.sum(count) * 100 if 0 in u else 0, 1)

                if len(params['sample']['sampling']['method']) == 1:
                    if params['sample']['sampling']['method'][0] == 'min_annotated_percent':
                        if minimum_annotated_percent(target_background_percent, params['sample']['sampling']['map']):
                            val = compute_classes(dataset, samples_file, val_percent, val_sample_file,
                                                  data, target, metadata_idx, pixel_classes)
                            if val:
                                idx_samples_v += 1
                            else:
                                idx_samples += 1
                                added_samples += 1
                        else:
                            excl_samples += 1

                    if params['sample']['sampling']['method'][0] == 'class_proportion':
                        if class_proportion(target):
                            val = compute_classes(dataset, samples_file, val_percent, val_sample_file,
                                                  data, target, metadata_idx, pixel_classes)
                            if val:
                                idx_samples_v += 1
                            else:
                                idx_samples += 1
                                added_samples += 1
                        else:
                            excl_samples += 1

                if len(params['sample']['sampling']['method']) == 2:
                    if params['sample']['sampling']['method'][0] == 'min_annotated_percent':
                        if minimum_annotated_percent(target_background_percent, params['sample']['sampling']['map']):
                            if params['sample']['sampling']['method'][1] == 'class_proportion':
                                if class_proportion(target):
                                    val = compute_classes(dataset, samples_file, val_percent, val_sample_file,
                                                          data, target, metadata_idx, pixel_classes)
                                    if val:
                                        idx_samples_v += 1
                                    else:
                                        idx_samples += 1
                                        added_samples += 1
                                else:
                                    excl_samples += 1

                    elif params['sample']['sampling']['method'][0] == 'class_proportion':
                        if class_proportion(target):
                            if params['sample']['sampling']['method'][1] == 'min_annotated_percent':
                                if minimum_annotated_percent(target_background_percent,
                                                             params['sample']['sampling']['map']):
                                    val = compute_classes(dataset, samples_file, val_percent, val_sample_file,
                                                          data, target, metadata_idx, pixel_classes)
                                    if val:
                                        idx_samples_v += 1
                                    else:
                                        idx_samples += 1
                                        added_samples += 1
                                else:
                                    excl_samples += 1

                target_class_num = np.max(u)
                if num_classes < target_class_num:
                    num_classes = target_class_num

                _tqdm.set_postfix(Excld_samples=excl_samples,
                                  Added_samples=f'{added_samples}/{len(_tqdm) * len(range(0, w, dist_samples))}',
                                  Target_annot_perc=100 - target_background_percent)

    if dataset == 'tst':
        samples_count['tst'] = idx_samples
    else:
        samples_count['trn'] = idx_samples
        samples_count['val'] = idx_samples_v
    # return the appended samples count and number of classes.
    return samples_count, num_classes


def main(params):
    """
    Training and validation datasets preparation.
    :param params: (dict) Parameters found in the yaml config file.

    """
    now = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")
    bucket_file_cache = []

    assert params['global']['task'] == 'segmentation', f"images_to_samples.py isn't necessary when performing classification tasks"

    # SET BASIC VARIABLES AND PATHS. CREATE OUTPUT FOLDERS.
    bucket_name = params['global']['bucket_name']
    data_path = Path(params['global']['data_path'])
    Path.mkdir(data_path, exist_ok=True, parents=True)
    csv_file = params['sample']['prep_csv_file']
    val_percent = params['sample']['val_percent']
    samples_size = params["global"]["samples_size"]
    overlap = params["sample"]["overlap"]
    min_annot_perc = params['sample']['sampling']['map']
    num_bands = params['global']['number_of_bands']
    debug = get_key_def('debug_mode', params['global'], False)
    if debug:
        warnings.warn(f'Debug mode activate. Execution may take longer...')

    final_samples_folder = None
    if bucket_name:
        s3 = boto3.resource('s3')
        bucket = s3.Bucket(bucket_name)
        bucket.download_file(csv_file, 'samples_prep.csv')
        list_data_prep = read_csv('samples_prep.csv')
        if data_path:
            final_samples_folder = os.path.join(data_path, "samples")
        else:
            final_samples_folder = "samples"
        samples_folder = f'samples{samples_size}_overlap{overlap}_min-annot{min_annot_perc}_{num_bands}bands'  # TODO: validate this is preferred name structure

    else:
        list_data_prep = read_csv(csv_file)
        samples_folder = data_path.joinpath(f'samples{samples_size}_overlap{overlap}_min-annot{min_annot_perc}_{num_bands}bands')

    if samples_folder.is_dir():
        warnings.warn(f'Data path exists: {samples_folder}. Suffix will be added to directory name.')
        samples_folder = Path(str(samples_folder) + '_' + now)
    else:
        tqdm.write(f'Writing samples to {samples_folder}')
    Path.mkdir(samples_folder, exist_ok=False)  # FIXME: what if we want to append samples to existing hdf5?
    tqdm.write(f'Samples will be written to {samples_folder}\n\n')

    tqdm.write(f'\nSuccessfully read csv file: {Path(csv_file).stem}\nNumber of rows: {len(list_data_prep)}\nCopying first entry:\n{list_data_prep[0]}\n')
    ignore_index = get_key_def('ignore_index', params['training'], -1)

    for info in tqdm(list_data_prep, position=0, desc=f'Asserting existence of tif and gpkg files in csv'):
        assert Path(info['tif']).is_file(), f'Could not locate "{info["tif"]}". Make sure file exists in this directory.'
        assert Path(info['gpkg']).is_file(), f'Could not locate "{info["gpkg"]}". Make sure file exists in this directory.'
    if debug:
        for info in tqdm(list_data_prep, position=0, desc=f"Validating presence of {params['global']['num_classes']} "
                                                          f"classes in attribute \"{info['attribute_name']}\" for vector "
                                                          f"file \"{Path(info['gpkg']).stem}\""):
            validate_num_classes(info['gpkg'], params['global']['num_classes'], info['attribute_name'], ignore_index)
        with tqdm(list_data_prep, position=0, desc=f"Checking validity of features in vector files") as _tqdm:
            invalid_features = {}
            for info in _tqdm:
                # Extract vector features to burn in the raster image
                with fiona.open(info['gpkg'], 'r') as src:  # TODO: refactor as independent function
                    lst_vector = [vector for vector in src]
                shapes = lst_ids(list_vector=lst_vector, attr_name=info['attribute_name'])
                for index, item in enumerate(tqdm([v for vecs in shapes.values() for v in vecs], leave=False, position=1)):
                    # geom must be a valid GeoJSON geometry type and non-empty
                    geom, value = item
                    geom = getattr(geom, '__geo_interface__', None) or geom
                    if not is_valid_geom(geom):
                        gpkg_stem = str(Path(info['gpkg']).stem)
                        if gpkg_stem not in invalid_features.keys():  # create key with name of gpkg
                            invalid_features[gpkg_stem] = []
                        if lst_vector[index]["id"] not in invalid_features[gpkg_stem]:  # ignore feature is already appended
                            invalid_features[gpkg_stem].append(lst_vector[index]["id"])
            assert len(invalid_features.values()) == 0, f'Invalid geometry object(s) for "gpkg:ids": \"{invalid_features}\"'

    number_samples = {'trn': 0, 'val': 0, 'tst': 0}
    number_classes = 0

    # 'sampling' ordereddict validation
    check_sampling_dict()

    pixel_classes = {}
    # creates pixel_classes dict and keys
    for i in range(0, params['global']['num_classes'] + 1):
        pixel_classes.update({i: 0})
    pixel_classes.update({ignore_index: 0})  # FIXME: pixel_classes dict needs to be populated with classes obtained from target

    trn_hdf5, val_hdf5, tst_hdf5 = create_files_and_datasets(params, samples_folder)

    # For each row in csv: (1) burn vector file to raster, (2) read input raster image, (3) prepare samples
    with tqdm(list_data_prep, position=0, leave=False, desc=f'Preparing samples') as _tqdm:
        for info in _tqdm:
            _tqdm.set_postfix(
                OrderedDict(tif=f'{Path(info["tif"]).stem}', sample_size=params['global']['samples_size']))
            try:
                if bucket_name:
                    bucket.download_file(info['tif'], "Images/" + info['tif'].split('/')[-1])
                    info['tif'] = "Images/" + info['tif'].split('/')[-1]
                    if info['gpkg'] not in bucket_file_cache:
                        bucket_file_cache.append(info['gpkg'])
                        bucket.download_file(info['gpkg'], info['gpkg'].split('/')[-1])
                    info['gpkg'] = info['gpkg'].split('/')[-1]
                    if info['meta']:
                        if info['meta'] not in bucket_file_cache:
                            bucket_file_cache.append(info['meta'])
                            bucket.download_file(info['meta'], info['meta'].split('/')[-1])
                        info['meta'] = info['meta'].split('/')[-1]

                with rasterio.open(info['tif'], 'r') as raster:
                    # Burn vector file in a raster file
                    np_label_raster = vector_to_raster(vector_file=info['gpkg'],
                                                       input_image=raster,
                                                       attribute_name=info['attribute_name'],
                                                       fill=get_key_def('ignore_idx',
                                                                        get_key_def('training', params, {}), 0))
                    # Read the input raster image
                    np_input_image = image_reader_as_array(input_image=raster,
                                                           scale=get_key_def('scale_data', params['global'], None),
                                                           aux_vector_file=get_key_def('aux_vector_file',
                                                                                       params['global'], None),
                                                           aux_vector_attrib=get_key_def('aux_vector_attrib',
                                                                                         params['global'], None),
                                                           aux_vector_ids=get_key_def('aux_vector_ids',
                                                                                      params['global'], None),
                                                           aux_vector_dist_maps=get_key_def('aux_vector_dist_maps',
                                                                                            params['global'], True),
                                                           aux_vector_dist_log=get_key_def('aux_vector_dist_log',
                                                                                           params['global'], True),
                                                           aux_vector_scale=get_key_def('aux_vector_scale',
                                                                                        params['global'], None))

                # Mask the zeros from input image into label raster.
                if params['sample']['mask_reference']:
                    np_label_raster = mask_image(np_input_image, np_label_raster)

                if info['dataset'] == 'trn':
                    out_file = trn_hdf5
                    val_file = val_hdf5
                elif info['dataset'] == 'tst':
                    out_file = tst_hdf5
                else:
                    raise ValueError(f"Dataset value must be trn or val or tst. Provided value is {info['dataset']}")

                meta_map, metadata = get_key_def("meta_map", params["global"], {}), None
                if info['meta'] is not None and isinstance(info['meta'], str) and Path(info['meta']).is_file():
                    metadata = read_parameters(info['meta'])

                # FIXME: think this through. User will have to calculate the total number of bands including meta layers and
                #  specify it in yaml. Is this the best approach? What if metalayers are added on the fly ?
                input_band_count = np_input_image.shape[2] + MetaSegmentationDataset.get_meta_layer_count(meta_map)
                # FIXME: could this assert be done before getting into this big for loop?
                assert input_band_count == num_bands, \
                    f"The number of bands in the input image ({input_band_count}) and the parameter" \
                    f"'number_of_bands' in the yaml file ({params['global']['number_of_bands']}) should be identical"

                np_label_raster = np.reshape(np_label_raster, (np_label_raster.shape[0], np_label_raster.shape[1], 1))
                number_samples, number_classes = samples_preparation(np_input_image,
                                                                     np_label_raster,
                                                                     samples_size,
                                                                     overlap,
                                                                     number_samples,
                                                                     number_classes,
                                                                     out_file,
                                                                     val_percent,
                                                                     val_file,
                                                                     info['dataset'],
                                                                     pixel_classes,
                                                                     metadata)

                _tqdm.set_postfix(OrderedDict(number_samples=number_samples))
                out_file.flush()
            except Exception as e:
                warnings.warn(f'An error occurred while preparing samples with "{Path(info["tif"]).stem}" (tiff) and '
                              f'{Path(info["gpkg"]).stem} (gpkg). Error: "{e}"')
                continue

    trn_hdf5.close()
    val_hdf5.close()
    tst_hdf5.close()

    pixel_total = 0
    # adds up the number of pixels for each class in pixel_classes dict
    for i in pixel_classes:
        pixel_total += pixel_classes[i]

    # prints the proportion of pixels of each class for the samples created
    for i in pixel_classes:
        print('Pixels from class', i, ':', round((pixel_classes[i] / pixel_total) * 100, 1), '%')

    print("Number of samples created: ", number_samples)

    if bucket_name and final_samples_folder:
        print('Transfering Samples to the bucket')
        bucket.upload_file(samples_folder + "/trn_samples.hdf5", final_samples_folder + '/trn_samples.hdf5')
        bucket.upload_file(samples_folder + "/val_samples.hdf5", final_samples_folder + '/val_samples.hdf5')
        bucket.upload_file(samples_folder + "/tst_samples.hdf5", final_samples_folder + '/tst_samples.hdf5')

    print("End of process")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Sample preparation')
    parser.add_argument('ParamFile', metavar='DIR',
                        help='Path to training parameters stored in yaml')
    args = parser.parse_args()
    params = read_parameters(args.ParamFile)
    start_time = time.time()
    tqdm.write(f'\n\nStarting images to samples preparation with {args.ParamFile}\n\n')
    main(params)
    print("Elapsed time:{}".format(time.time() - start_time))
