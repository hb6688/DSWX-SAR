import os
import logging
import warnings

from abc import ABC, abstractmethod
from collections.abc import Iterator
import h5py
import mimetypes
import numpy as np
from osgeo import gdal
from osgeo.gdal import Dataset
from pathlib import Path
import rasterio
from rasterio.transform import Affine
from rasterio.windows import Window
from typing import Any

from mosaic_rtc_burst import majority_element, mosaic_single_output_file
from dswx_sar_util import change_epsg_tif
from dswx_sar.dswx_geogrid import DSWXGeogrid
from dswx_ni_runconfig import _get_parser, RunConfig


logger = logging.getLogger('dswx_sar')


class DataReader(ABC):
    def __init__(self, row_blk_size: int, col_blk_size: int):
        self.row_blk_size = row_blk_size
        self.col_blk_size = col_blk_size

    @abstractmethod
    def process_rtc_hdf5(self, input_list: list) -> Any:
        pass


class RTCReader(DataReader):
    def __init__(self, row_blk_size: int, col_blk_size: int):
        super().__init__(row_blk_size, col_blk_size)

    def process_rtc_hdf5(
            self,
            input_list: list,
            output_dir: str,
            scratch_dir: str,
            mosaic_mode: str,
            mosaic_prefix: str):
        """Read data from input HDF5s in blocks and generate mosaicked output
           Geotiff

        Parameters
        ----------
        input_list: list
            The HDF5 file paths of input RTCs to be mosaicked.
        output_dir: str
            Directory which stores the mosaic output Geotiff
        scratch_dir: str
            Directory which stores the temporary files
        mosaic_mode: str
            Mosaic algorithm mode choice in 'average', 'first',
            or 'burst_center'
        mosaic_prefix: str
            Mosaicked output file name prefix
        """
        # Extract polarizations
        pols_rtc = self.extract_nisar_polarization(input_list)

        # Generate data paths
        data_path = self.generate_nisar_dataset_name(pols_rtc)

        # Generate layover mask path
        layover_mask_name = 'layoverShadowMask'
        layover_path = str(self.generate_nisar_layover_name(layover_mask_name))

        # Collect EPSG
        epsg_array, epsg_same_flag = self.get_nisar_epsg(input_list)

        # To Do: Read and write to geotiff
        # 1. If EPSG are the same between inputs, then write it to output
        #    mosaicked Geotiff directly from input HDF5
        # 2. Otherwise, write all to intermediate Geotiff first and re-use
        #    existing functions to reproject data and create mosaicked output
        #    from intermediate Geotiffs

        # Currently, intermediate Geotiffs are created regardless of EPSG
        layover_exist, geogrid_in = self.write_rtc_geotiff(
            input_list,
            output_dir,
            scratch_dir,
            epsg_array,
            data_path,
            layover_path,
            )

        # Mosaicking intermediate input geotiffs
        nlooks_list = []

        self.mosaic_rtc_geotiff(
            input_list,
            data_path,
            output_dir,
            scratch_dir,
            geogrid_in,
            nlooks_list,
            mosaic_mode,
            mosaic_prefix,
            layover_exist,
        )

    def write_rtc_geotiff(
            self,
            input_list: list,
            output_dir: str,
            scratch_dir: str,
            epsg_array: np.ndarray,
            data_path: list,
            layover_path: list,
            ):
        """ Create intermediate Geotiffs from a list of input RTCs

        Parameters
        ----------
        input_list: list
            The HDF5 file paths of input RTCs to be mosaicked.
        output_dir: str
            Directory which stores the mosaic output Geotiff
        scratch_dir: str
            Directory which stores the temporary files
        epsg_array: array of int
            EPSG of each of the RTC input HDF5
        data_path: list
            RTC dataset path within the HDF5 input file
        layover_path: str
            layoverShadowMask layer dataset path

        Returns
        -------
        layover_exist: bool
            Boolean which indicates if a layoverShadowMask layer exists
            in input RTC
        geogrid_in: DSWXGeogrid object
            A dataclass object  representing the geographical grid
            configuration for an RTC (Radar Terrain Correction) run.
        """

        # Reproject geotiff
        most_freq_epsg = majority_element(epsg_array)
        designated_value = np.float32(500)

        # Create intermediate input Geotiffs
        for input_idx, input_rtc in enumerate(input_list):
            # Extract file names
            output_prefix = self.extract_file_name(input_rtc)

            # Read geotranform data
            geotransform, crs = self.read_geodata_hdf5(input_rtc)

            # Read metadata
            dswx_metadata_dict = self.read_metadata_hdf5(input_rtc)

            # Create Intermediate Geotiffs for each input GCOV file
            for path_idx, dataset_path in enumerate(data_path):
                data_name = Path(dataset_path).name[:2]
                dataset = f'HDF5:{input_rtc}:/{dataset_path}'
                output_gtiff = f'{scratch_dir}/{output_prefix}_{data_name}.tif'
                h5_ds = gdal.Open(dataset, gdal.GA_ReadOnly)

                num_cols = h5_ds.RasterXSize
                num_rows = h5_ds.RasterYSize

                row_blk_size = self.row_blk_size
                col_blk_size = self.col_blk_size

                self.read_write_rtc(
                    h5_ds,
                    output_gtiff,
                    num_rows,
                    num_cols,
                    row_blk_size,
                    col_blk_size,
                    designated_value,
                    geotransform,
                    crs,
                    dswx_metadata_dict,
                    )

        geogrid_in = DSWXGeogrid()
        # Loop through EPSG (input files)
        for input_idx, input_rtc in enumerate(input_list):
            input_prefix = self.extract_file_name(input_rtc)
            # Check if the RTC has the same EPSG with the reference.
            if epsg_array[input_idx] != most_freq_epsg:
                for idx, dataset_path in enumerate(data_path):
                    data_name = Path(dataset_path).name[:2]
                    input_gtiff = \
                        f'{scratch_dir}/{input_prefix}_{data_name}.tif'
                    temp_gtiff = \
                        f'{scratch_dir}/{input_prefix}_temp_{data_name}.tif'

                    # Change EPSG
                    change_epsg_tif(
                        input_tif=input_gtiff,
                        output_tif=temp_gtiff,
                        epsg_output=most_freq_epsg,
                        output_nodata=255,
                    )

                    # Update geogrid
                    geogrid_in.update_geogrid(output_gtiff)

                    # Replace input file with output temp file
                    os.replace(temp_gtiff, input_gtiff)
            else:
                for idx, dataset_path in enumerate(data_path):
                    data_name = Path(dataset_path).name[:2]
                    output_gtiff = \
                        f'{scratch_dir}/{input_prefix}_{data_name}.tif'

                    # Update geogrid
                    geogrid_in.update_geogrid(output_gtiff)

        # Generate Layover Shadow Mask Geotiff
        for input_idx, input_rtc in enumerate(input_list):
            layover_data = f'HDF5:{input_rtc}:/{layover_path}'
            h5_layover = gdal.Open(layover_data, gdal.GA_ReadOnly)

            # Check if layoverShadowMask layer exists:
            if h5_layover is None:
                warnings.warn(f'\nDataset at {layover_data} does not exist or '
                              'cannot be opened.', RuntimeWarning)
                layover_exist = False
                break
            else:
                layover_exist = True

                output_prefix = self.extract_file_name(input_rtc)
                output_layover_gtiff = \
                    f'{scratch_dir}/{output_prefix}_layover.tif'
                num_cols = h5_layover.RasterXSize
                col_blk_size = self.col_blk_size

                self.read_write_rtc(
                    h5_layover,
                    output_layover_gtiff,
                    num_rows,
                    num_cols,
                    row_blk_size,
                    col_blk_size,
                    designated_value,
                    geotransform,
                    crs,
                    dswx_metadata_dict,
                )

                # Change EPSG of layOverMask if necessary
                if epsg_array[input_idx] != most_freq_epsg:
                    input_prefix = self.extract_file_name(input_rtc)
                    input_layover_gtiff = \
                        f'{scratch_dir}/{input_prefix}_layover.tif'
                    temp_layover_gtiff = \
                        f'{scratch_dir}/{input_prefix}_temp_layover.tif'

                    change_epsg_tif(
                        input_tif=input_layover_gtiff,
                        output_tif=temp_layover_gtiff,
                        epsg_output=most_freq_epsg,
                        output_nodata=255,
                    )

                    geogrid_in.update_geogrid(output_layover_gtiff)

                    # Replace input file with output temp file
                    os.replace(temp_layover_gtiff, input_layover_gtiff)
                else:
                    geogrid_in.update_geogrid(output_layover_gtiff)

        return layover_exist, geogrid_in

    def mosaic_rtc_geotiff(
        self,
        input_list: list,
        data_path: list,
        output_dir: str,
        scratch_dir: str,
        geogrid_in: DSWXGeogrid,
        nlooks_list: list,
        mosaic_mode: str,
        mosaic_prefix: str,
        layover_exist: bool,
    ):
        """ Create mosaicked output Geotiff from a list of input RTCs

        Parameters
        ----------
        input_list: list
            The HDF5 file paths of input RTCs to be mosaicked.
        data_path: list
            RTC dataset path within the HDF5 input file
        output_dir: str
            Directory which stores the mosaic output Geotiff
        scratch_dir: str
            Directory which stores the temporary files
        geogrid_in: DSWXGeogrid object
            A dataclass object  representing the geographical grid
            configuration for an RTC (Radar Terrain Correction) run.
        nlooks_list: list
            List of the nlooks raster that corresponds to list_rtc
        mosaic_mode: str
            Mosaic algorithm mode choice in 'average', 'first',
            or 'burst_center'
        mosaic_prefix: str
            Mosaicked output file name prefix
        layover_exist: bool
            Boolean which indicates if a layoverShadowMask layer
            exists in input RTC
        """
        for idx, dataset_path in enumerate(data_path):
            data_name = Path(dataset_path).name[:2]
            input_gtiff_list = []
            for input_idx, input_rtc in enumerate(input_list):
                input_prefix = self.extract_file_name(input_rtc)
                input_gtiff = f'{scratch_dir}/{input_prefix}_{data_name}.tif'
                input_gtiff_list = np.append(input_gtiff_list, input_gtiff)

            # Mosaic dataset of same polarization into a single Geotiff
            output_mosaic_gtiff = \
                f'{output_dir}/{mosaic_prefix}_{data_name}.tif'
            mosaic_single_output_file(
                input_gtiff_list,
                nlooks_list,
                output_mosaic_gtiff,
                mosaic_mode,
                scratch_dir=scratch_dir,
                geogrid_in=geogrid_in,
                temp_files_list=None,
                )

        # Mosaic layover shadow mask
        if layover_exist:
            layover_gtiff_list = []
            for input_idx, input_rtc in enumerate(input_list):
                input_prefix = self.extract_file_name(input_rtc)
                layover_gtiff = f'{scratch_dir}/{input_prefix}_layover.tif'
                layover_gtiff_list = np.append(layover_gtiff_list,
                                               layover_gtiff)

            layover_mosaic_gtiff = f'{output_dir}/{mosaic_prefix}_layover.tif'

            mosaic_single_output_file(
                layover_gtiff_list,
                nlooks_list,
                layover_mosaic_gtiff,
                mosaic_mode,
                scratch_dir=scratch_dir,
                geogrid_in=geogrid_in,
                temp_files_list=None,
            )

    def extract_file_name(self, input_rtc):
        """Extract file name identifier from input file name

        Parameters
        ----------
        input_rtc: str
            The HDF5 RTC input file path

        Returns
        -------
        file_name: str
            file name identifier
        """

        # Check if the file exists
        if not os.path.exists(input_rtc):
            raise FileNotFoundError(f"The file '{input_rtc}' does not exist.")

        file_name = Path(input_rtc).stem.split('-')[0]

        return file_name

    def extract_nisar_polarization(self, input_list):
        """Extract input RTC dataset polarizations

        Parameters
        ----------
        input_list: list
            The HDF5 file paths of input RTCs to be mosaicked.

        Returns
        -------
        polarizations: list of str
            All dataset polarizations listed in the input HDF5 file
        """

        pol_list_path = \
            '/science/LSAR/GCOV/grids/frequencyA/listOfPolarizations'
        polarizations = []
        pols_rtc = []
        for input_idx, input_rtc in enumerate(input_list):
            # Check if the file exists
            if not os.path.exists(input_rtc):
                raise FileNotFoundError(
                    f"The file '{input_rtc}' does not exist.")
            with h5py.File(input_rtc, 'r') as src_h5:
                pols = np.sort(src_h5[pol_list_path][()])
                if len(polarizations) == 0:
                    polarizations = pols.copy()
                elif not np.all(polarizations == pols):
                    raise ValueError(
                        "Polarizations of multiple RTC files "
                        "are not consistent.")

        for pol_idx, pol in enumerate(polarizations):
            pols_rtc = np.append(pols_rtc, pol.decode('utf-8'))

        return pols_rtc

    def generate_nisar_dataset_name(self, data_name: str | list[str]):
        """Generate dataset paths

        Parameters
        ----------
        data_name: str or list of str
            All dataset polarizations listed in the input HDF5 file

        Returns
        -------
        data_path: np.ndarray of str
            RTC dataset path within the HDF5 input file
        """

        if isinstance(data_name, str):
            data_name = [data_name]

        group = '/science/LSAR/GCOV/grids/frequencyA/'
        data_path = []
        for name_idx, dname in enumerate(data_name):
            data_path = np.append(data_path, f'{group}{dname * 2}')

        return data_path

    def generate_nisar_layover_name(self, layover_name: str):
        """Generate layOverShadowMask dataset path

        Parameters
        ----------
        layover_name: str
            Name of layover and shadow Mask layer in the input HDF5 file

        Returns
        -------
        data_path: str
            RTC dataset path within the HDF5 input file
        """
        group = '/science/LSAR/GCOV/grids/frequencyA/'

        data_path = f'{group}{layover_name}'

        return data_path

    def get_nisar_epsg(self, input_list):
        """extract data from RTC Geo information and store it as a dictionary

        parameters
        ----------
        input_list: list
            The HDF5 file paths of input RTCs to be mosaicked.

        Returns
        -------
        epsg_array: array of int
            EPSG of each of the RTC input HDF5
        epsg_same_flag: bool
            A flag which indicates whether all input EPSG are the same
            if True, all input EPSG are the same and vice versa.
        """
        proj = '/science/LSAR/GCOV/grids/frequencyA/projection'

        epsg_array = np.zeros(len(input_list), dtype=int)
        for input_idx, input_rtc in enumerate(input_list):
            with h5py.File(input_rtc, 'r') as src_h5:
                epsg_array[input_idx] = src_h5[proj][()]

        if (epsg_array == epsg_array[0]).all():
            epsg_same_flag = True
        else:
            epsg_same_flag = False

        return epsg_array, epsg_same_flag

    def read_write_rtc(
            self,
            h5_ds: Dataset,
            output_gtiff,
            num_rows: int,
            num_cols: int,
            row_blk_size: int,
            col_blk_size: int,
            designated_value: np.float32,
            geotransform: Affine,
            crs: str,
            dswx_metadata_dict: dict):
        """Read an level-2 RTC product in HDF5 format and writ it out in
        GeoTiff format in data blocks defined by row_blk_size and col_blk_size.

        Parameters
        ----------
        h5_ds: GDAL Dataset
            GDAL dataset object to be processed
        output_gtiff: str
        Output Geotiff file path and name
        num_rows: int
        The number of rows (height) of the output Geotiff.
        num_cols: int
        The number of columns (width) of the output Geotiff.
        row_blk_size: int
        The number of rows to read each time from the dataset.
        col_blk_size: int
        The number of columns to read each time from the dataset
        designated_value: np.float32
            Identify Inf in the dataset and replace them with
            a designated value
        geotransform: Affine Transformation object
            Transformation matrix which maps pixel locations in (row, col)
            coordinates to (x, y) spatial positions.
        crs: str
            Coordinate Reference System object in EPSG representation
        dswx_metadata_dict: dictionary
            This dictionary metadata extracted from input RTC
        """
        row_blk_size = self.row_blk_size
        col_blk_size = self.col_blk_size

        with rasterio.open(
            output_gtiff,
            'w',
            driver='GTiff',
            height=num_rows,
            width=num_cols,
            count=1,
            dtype='float32',
            crs=crs,
            transform=geotransform,
            compress='DEFLATE',
        ) as dst:
            for idx_y, slice_row in enumerate(slice_gen(num_rows,
                                                        row_blk_size)):
                row_slice_size = slice_row.stop - slice_row.start
                for idx_x, slice_col in enumerate(slice_gen(num_cols,
                                                            col_blk_size)):
                    col_slice_size = slice_col.stop - slice_col.start

                    ds_blk = h5_ds.ReadAsArray(
                        slice_col.start,
                        slice_row.start,
                        col_slice_size,
                        row_slice_size,
                    )

                    # Replace Inf values with a designated value: 500
                    ds_blk[np.isinf(ds_blk)] = designated_value
                    ds_blk[ds_blk > designated_value] = designated_value
                    ds_blk[ds_blk == 0] = np.nan

                    dst.write(
                        ds_blk,
                        1,
                        window=Window(
                            slice_col.start,
                            slice_row.start,
                            col_slice_size,
                            row_slice_size
                        )
                    )

            dst.update_tags(**dswx_metadata_dict)

    def read_geodata_hdf5(self, input_rtc):
        """extract data from RTC Geo information and store it as a dictionary

        parameters
        ----------
        input_rtc: str
            The HDF5 RTC input file path

        Returns
        -------
        geotransform: Affine Transformation object
            Transformation matrix which maps pixel locations in (row, col)
            coordinates to (x, y) spatial positions.
        crs: str
            Coordinate Reference System object in EPSG representation
        """
        frequency_a_path = '/science/LSAR/GCOV/grids/frequencyA'
        geo_name_mapping = {
            'xcoord': f'{frequency_a_path}/xCoordinates',
            'ycoord': f'{frequency_a_path}/yCoordinates',
            'xposting': f'{frequency_a_path}/xCoordinateSpacing',
            'yposting': f'{frequency_a_path}/yCoordinateSpacing',
            'proj': f'{frequency_a_path}/projection'
        }

        with h5py.File(input_rtc, 'r') as src_h5:
            xmin = src_h5[f"{geo_name_mapping['xcoord']}"][:][0]
            ymin = src_h5[f"{geo_name_mapping['ycoord']}"][:][0]
            xres = src_h5[f"{geo_name_mapping['xposting']}"][()]
            yres = src_h5[f"{geo_name_mapping['yposting']}"][()]
            epsg = src_h5[f"{geo_name_mapping['proj']}"][()]

        # Geo transformation
        geotransform = Affine.translation(
            xmin - xres/2, ymin - yres/2) * Affine.scale(xres, yres)

        # Coordinate Reference System
        crs = f'EPSG:{epsg}'

        return geotransform, crs

    def read_metadata_hdf5(self, input_rtc):
        """Read NISAR Level-2 GCOV metadata

        Parameters
        ----------
        input_rtc: str
            The HDF5 RTC input file path

        Returns
        -------
        dswx_metadata_dict: dictionary
            RTC metadata dictionary. Will be written into output GeoTIFF.

        """
        id_path = '/science/LSAR/identification'
        meta_path = '/science/LSAR/GCOV/metadata'
        # Metadata Name Dictionary
        dswx_meta_mapping = {
            'RTC_ORBIT_PASS_DIRECTION': f'{id_path}/orbitPassDirection',
            'RTC_LOOK_DIRECTION': f'{id_path}/lookDirection',
            'RTC_PRODUCT_VERSION': f'{id_path}/productVersion',
            'RTC_SENSING_START_TIME': f'{id_path}/zeroDopplerStartTime',
            'RTC_SENSING_END_TIME': f'{id_path}/zeroDopplerEndTime',
            'RTC_FRAME_NUMBER': f'{id_path}/frameNumber',
            'RTC_TRACK_NUMBER': f'{id_path}/trackNumber',
            'RTC_ABSOLUTE_ORBIT_NUMBER': f'{id_path}/absoluteOrbitNumber',
            'RTC_INPUT_L1_SLC_GRANULES':
                f'{meta_path}/processingInformation/inputs/l1SlcGranules',
            }

        with h5py.File(input_rtc, 'r') as src_h5:
            orbit_pass_dir = src_h5[
                dswx_meta_mapping['RTC_ORBIT_PASS_DIRECTION']][()].decode()
            look_dir = src_h5[
                dswx_meta_mapping['RTC_LOOK_DIRECTION']][()].decode()
            prod_ver = src_h5[
                dswx_meta_mapping['RTC_PRODUCT_VERSION']][()].decode()
            zero_dopp_start = src_h5[
                dswx_meta_mapping['RTC_SENSING_START_TIME']][()].decode()
            zero_dopp_end = src_h5[
                dswx_meta_mapping['RTC_SENSING_END_TIME']][()].decode()
            frame_number = src_h5[
                dswx_meta_mapping['RTC_FRAME_NUMBER']][()]
            track_number = src_h5[
                dswx_meta_mapping['RTC_TRACK_NUMBER']][()]
            abs_orbit_number = src_h5[
                dswx_meta_mapping['RTC_ABSOLUTE_ORBIT_NUMBER']][()]
            input_slc_granules = src_h5[
                dswx_meta_mapping['RTC_INPUT_L1_SLC_GRANULES']][(0)].decode()

        dswx_metadata_dict = {
            'ORBIT_PASS_DIRECTION': orbit_pass_dir,
            'LOOK_DIRECTION': look_dir,
            'INPUT_L1_SLC_GRANULES': input_slc_granules,
            'PRODUCT_VERSION': prod_ver,
            'ZERO_DOPPLER_START_TIME': zero_dopp_start,
            'ZERO_DOPPLER_END_TIME': zero_dopp_end,
            'FRAME_NUMBER': frame_number,
            'TRACK_NUMBER': track_number,
            'ABSOLUTE_ORBIT_NUMBER': abs_orbit_number,
        }

        return dswx_metadata_dict


def slice_gen(total_size: int,
              batch_size: int,
              combine_rem: bool = True) -> Iterator[slice]:
    """Generate slices with size defined by batch_size.

    Parameters
    ----------
    total_size: int
        size of data to be manipulated by slice_gen
    batch_size: int
        designated data chunk size in which data is sliced into.
    combine_rem: bool
        Combine the remaining values with the last complete block if 'True'.
        If False, ignore the remaining values
        Default = 'True'

    Yields
    ------
    slice: slice obj
        Iterable slices of data with specified input batch size,
        bounded by start_idx and stop_idx.
    """
    num_complete_blks = total_size // batch_size
    num_total_complete = num_complete_blks * batch_size
    num_rem = total_size - num_total_complete

    if combine_rem and num_rem > 0:
        for start_idx in range(0, num_total_complete - batch_size, batch_size):
            stop_idx = start_idx + batch_size
            yield slice(start_idx, stop_idx)

        last_blk_start = num_total_complete - batch_size
        last_blk_stop = total_size
        yield slice(last_blk_start, last_blk_stop)
    else:
        for start_idx in range(0, num_total_complete, batch_size):
            stop_idx = start_idx + batch_size
            yield slice(start_idx, stop_idx)


def run(cfg):
    """Generate mosaic workflow with user-defined args stored
    in dictionary runconfig 'cfg'

    Parameters:
    -----------
    cfg: RunConfig
        RunConfig object with user runconfig options
    """

    # Mosaicking parameters
    processing_cfg = cfg.groups.processing

    input_list = cfg.groups.input_file_group.input_file_path

    mosaic_cfg = processing_cfg.mosaic
    mosaic_mode = mosaic_cfg.mosaic_mode
    mosaic_prefix = mosaic_cfg.mosaic_prefix

    scratch_dir = cfg.groups.product_path_group.scratch_path
    os.makedirs(scratch_dir, exist_ok=True)

    row_blk_size = mosaic_cfg.read_row_blk_size
    col_blk_size = mosaic_cfg.read_col_blk_size

    # Create reader object
    reader = RTCReader(
        row_blk_size=row_blk_size,
        col_blk_size=col_blk_size,
    )

    # Mosaic input RTC into output Geotiff
    reader.process_rtc_hdf5(
        input_list,
        scratch_dir,
        scratch_dir,
        mosaic_mode,
        mosaic_prefix,
    )


if __name__ == "__main__":
    '''Run mosaic rtc products from command line'''
    # load arguments from command line
    parser = _get_parser()

    # parse arguments
    args = parser.parse_args()

    mimetypes.add_type("text/yaml", ".yaml", strict=True)

    cfg = RunConfig.load_from_yaml(args.input_yaml[0], 'dswx_ni', args)

    # Run Mosaic RTC workflow
    run(cfg)
