from __future__ import division

# Documentation available here:
# http://www.trackvis.org/docs/?subsect=fileformat

import struct
import os
import warnings

import numpy as np

from nibabel.openers import Opener
from nibabel.volumeutils import (native_code, swapped_code)

from nibabel.streamlines.base_format import StreamlinesFile
from nibabel.streamlines.base_format import DataError, HeaderError, HeaderWarning
from nibabel.streamlines.base_format import Streamlines, LazyStreamlines
from nibabel.streamlines.header import Field

from nibabel.streamlines.utils import get_affine_from_reference

# Definition of trackvis header structure.
# See http://www.trackvis.org/docs/?subsect=fileformat
# See http://docs.scipy.org/doc/numpy/reference/arrays.dtypes.html
header_1_dtd = [(Field.MAGIC_NUMBER, 'S6'),
                (Field.DIMENSIONS, 'h', 3),
                (Field.VOXEL_SIZES, 'f4', 3),
                (Field.ORIGIN, 'f4', 3),
                (Field.NB_SCALARS_PER_POINT, 'h'),
                ('scalar_name', 'S20', 10),
                (Field.NB_PROPERTIES_PER_STREAMLINE, 'h'),
                ('property_name', 'S20', 10),
                ('reserved', 'S508'),
                (Field.VOXEL_ORDER, 'S4'),
                ('pad2', 'S4'),
                ('image_orientation_patient', 'f4', 6),
                ('pad1', 'S2'),
                ('invert_x', 'S1'),
                ('invert_y', 'S1'),
                ('invert_z', 'S1'),
                ('swap_xy', 'S1'),
                ('swap_yz', 'S1'),
                ('swap_zx', 'S1'),
                (Field.NB_STREAMLINES, 'i4'),
                ('version', 'i4'),
                ('hdr_size', 'i4'),
                ]

# Version 2 adds a 4x4 matrix giving the affine transformtation going
# from voxel coordinates in the referenced 3D voxel matrix, to xyz
# coordinates (axes L->R, P->A, I->S). If (0 based) value [3, 3] from
# this matrix is 0, this means the matrix is not recorded.
header_2_dtd = [(Field.MAGIC_NUMBER, 'S6'),
                (Field.DIMENSIONS, 'h', 3),
                (Field.VOXEL_SIZES, 'f4', 3),
                (Field.ORIGIN, 'f4', 3),
                (Field.NB_SCALARS_PER_POINT, 'h'),
                ('scalar_name', 'S20', 10),
                (Field.NB_PROPERTIES_PER_STREAMLINE, 'h'),
                ('property_name', 'S20', 10),
                (Field.to_world_space, 'f4', (4, 4)),  # new field for version 2
                ('reserved', 'S444'),
                (Field.VOXEL_ORDER, 'S4'),
                ('pad2', 'S4'),
                ('image_orientation_patient', 'f4', 6),
                ('pad1', 'S2'),
                ('invert_x', 'S1'),
                ('invert_y', 'S1'),
                ('invert_z', 'S1'),
                ('swap_xy', 'S1'),
                ('swap_yz', 'S1'),
                ('swap_zx', 'S1'),
                (Field.NB_STREAMLINES, 'i4'),
                ('version', 'i4'),
                ('hdr_size', 'i4'),
                ]

# Full header numpy dtypes
header_1_dtype = np.dtype(header_1_dtd)
header_2_dtype = np.dtype(header_2_dtd)


class TrkReader(object):
    ''' Convenience class to encapsulate TRK file format.

    Parameters
    ----------
    fileobj : string or file-like object
        If string, a filename; otherwise an open file-like object
        pointing to TRK file (and ready to read from the beginning
        of the TRK header)

    Note
    ----
    TrackVis (so its file format: TRK) considers the streamline coordinate
    (0,0,0) to be in the corner of the voxel whereas NiBabel's streamlines
    internal representation (Voxel space) assume (0,0,0) to be in the
    center of the voxel.

    Thus, streamlines are shifted of half a voxel on load and are shifted
    back on save.
    '''
    def __init__(self, fileobj):
        self.fileobj = fileobj

        with Opener(self.fileobj) as f:
            # Read header
            header_str = f.read(header_2_dtype.itemsize)
            header_rec = np.fromstring(string=header_str, dtype=header_2_dtype)

            if header_rec['version'] == 1:
                header_rec = np.fromstring(string=header_str, dtype=header_1_dtype)
            elif header_rec['version'] == 2:
                pass  # Nothing more to do
            else:
                raise HeaderError('NiBabel only supports versions 1 and 2.')

            # Convert the first record of `header_rec` into a dictionnary
            self.header = dict(zip(header_rec.dtype.names, header_rec[0]))

            # Check endianness
            self.endianness = native_code
            if self.header['hdr_size'] != TrkFile.HEADER_SIZE:
                self.endianness = swapped_code

                # Swap byte order
                self.header = dict(zip(header_rec.dtype.names, header_rec[0].newbyteorder()))
                if self.header['hdr_size'] != TrkFile.HEADER_SIZE:
                    raise HeaderError('Invalid hdr_size: {0} instead of {1}'.format(self.header['hdr_size'], TrkFile.HEADER_SIZE))

            # By default, the voxel order is LPS.
            # http://trackvis.org/blog/forum/diffusion-toolkit-usage/interpretation-of-track-point-coordinates
            if self.header[Field.VOXEL_ORDER] == b"":
                warnings.warn(("Voxel order is not specified, will assume"
                               " 'LPS' since it is Trackvis software's"
                               " default."), HeaderWarning)
                self.header[Field.VOXEL_ORDER] = b"LPS"

            # Keep the file position where the data begin.
            self.offset_data = f.tell()

    def __iter__(self):
        i4_dtype = np.dtype(self.endianness + "i4")
        f4_dtype = np.dtype(self.endianness + "f4")

        with Opener(self.fileobj) as f:
            start_position = f.tell()

            nb_pts_and_scalars = int(3 + self.header[Field.NB_SCALARS_PER_POINT])
            pts_and_scalars_size = int(nb_pts_and_scalars * f4_dtype.itemsize)

            slice_pts_and_scalars = lambda data: (data, [])
            if self.header[Field.NB_SCALARS_PER_POINT] > 0:
                # This is faster than `np.split`
                slice_pts_and_scalars = lambda data: (data[:, :3], data[:, 3:])

            # Using np.fromfile would be faster, but does not support StringIO
            read_pts_and_scalars = lambda nb_pts: slice_pts_and_scalars(np.ndarray(shape=(nb_pts, nb_pts_and_scalars),
                                                                                   dtype=f4_dtype,
                                                                                   buffer=f.read(nb_pts * pts_and_scalars_size)))

            properties_size = int(self.header[Field.NB_PROPERTIES_PER_STREAMLINE] * f4_dtype.itemsize)
            read_properties = lambda: []
            if self.header[Field.NB_PROPERTIES_PER_STREAMLINE] > 0:
                read_properties = lambda: np.fromstring(f.read(properties_size),
                                                        dtype=f4_dtype,
                                                        count=self.header[Field.NB_PROPERTIES_PER_STREAMLINE])

            # Set the file position at the beginning of the data.
            f.seek(self.offset_data, os.SEEK_SET)

            # If 'count' field is 0, i.e. not provided, we have to loop until the EOF.
            nb_streamlines = self.header[Field.NB_STREAMLINES]
            if nb_streamlines == 0:
                nb_streamlines = np.inf

            i = 0
            while i < nb_streamlines:
                nb_pts_str = f.read(i4_dtype.itemsize)

                # Check if we reached EOF
                if len(nb_pts_str) == 0:
                    break

                # Read number of points of the next streamline.
                nb_pts = struct.unpack(i4_dtype.str[:-1], nb_pts_str)[0]

                # Read streamline's data
                pts, scalars = read_pts_and_scalars(nb_pts)
                properties = read_properties()

                # TRK's streamlines are in 'voxelmm' space, we send them to voxel space.
                pts = pts / self.header[Field.VOXEL_SIZES]
                # TrackVis considers coordinate (0,0,0) to be the corner of the
                # voxel whereas streamlines returned assume (0,0,0) to be the
                # center of the voxel. Thus, streamlines are shifted of half
                #a voxel.
                pts -= np.array(self.header[Field.VOXEL_SIZES])/2.

                yield pts, scalars, properties
                i += 1

            # In case the 'count' field was not provided.
            self.header[Field.NB_STREAMLINES] = i

            # Set the file position where it was (in case it was already open).
            f.seek(start_position, os.SEEK_CUR)


class TrkWriter(object):
    @classmethod
    def create_empty_header(cls):
        ''' Return an empty compliant TRK header. '''
        header = np.zeros(1, dtype=header_2_dtype)

        #Default values
        header[Field.MAGIC_NUMBER] = TrkFile.MAGIC_NUMBER
        header[Field.VOXEL_SIZES] = (1, 1, 1)
        header[Field.DIMENSIONS] = (1, 1, 1)
        header[Field.to_world_space] = np.eye(4)
        header['version'] = 2
        header['hdr_size'] = TrkFile.HEADER_SIZE

        return header

    def __init__(self, fileobj, header):
        self.header = self.create_empty_header()

        # Override hdr's fields by those contain in `header`.
        for k, v in header.extra.items():
            if k in header_2_dtype.fields.keys():
                self.header[k] = v

        self.header[Field.NB_STREAMLINES] = 0
        if header.nb_streamlines is not None:
            self.header[Field.NB_STREAMLINES] = header.nb_streamlines

        self.header[Field.NB_SCALARS_PER_POINT] = header.nb_scalars_per_point
        self.header[Field.NB_PROPERTIES_PER_STREAMLINE] = header.nb_properties_per_streamline
        self.header[Field.VOXEL_SIZES] = header.voxel_sizes
        self.header[Field.to_world_space] = header.to_world_space
        self.header[Field.VOXEL_ORDER] = header.voxel_order

        # Keep counts for correcting incoherent fields or warn.
        self.nb_streamlines = 0
        self.nb_points = 0
        self.nb_scalars = 0
        self.nb_properties = 0

        # Write header
        self.file = Opener(fileobj, mode="wb")
        # Keep track of the beginning of the header.
        self.beginning = self.file.tell()
        self.file.write(self.header[0].tostring())

    def write(self, streamlines):
        i4_dtype = np.dtype("i4")
        f4_dtype = np.dtype("f4")

        for points, scalars, properties in streamlines:
            if len(scalars) > 0 and len(scalars) != len(points):
                raise DataError("Missing scalars for some points!")

            points = np.array(points, dtype=f4_dtype)
            scalars = np.array(scalars, dtype=f4_dtype).reshape((len(points), -1))
            properties = np.array(properties, dtype=f4_dtype)

            # TRK's streamlines need to be in 'voxelmm' space
            points = points * self.header[Field.VOXEL_SIZES]
            # TrackVis considers coordinate (0,0,0) to be the corner of the
            # voxel whereas streamlines passed in parameters assume (0,0,0)
            # to be the center of the voxel. Thus, streamlines are shifted of
            # half a voxel.
            points += np.array(self.header[Field.VOXEL_SIZES])/2.

            data = struct.pack(i4_dtype.str[:-1], len(points))
            data += np.concatenate((points, scalars), axis=1).tostring()
            data += properties.tostring()
            self.file.write(data)

            self.nb_streamlines += 1
            self.nb_points += len(points)
            self.nb_scalars += scalars.size
            self.nb_properties += len(properties)

        # Either correct or warn if header and data are incoherent.
        #TODO: add a warn option as a function parameter
        nb_scalars_per_point = self.nb_scalars / self.nb_points
        nb_properties_per_streamline = self.nb_properties / self.nb_streamlines

        # Check for errors
        if nb_scalars_per_point != int(nb_scalars_per_point):
            raise DataError("Nb. of scalars differs from one point to another!")

        if nb_properties_per_streamline != int(nb_properties_per_streamline):
            raise DataError("Nb. of properties differs from one streamline to another!")

        self.header[Field.NB_STREAMLINES] = self.nb_streamlines
        self.header[Field.NB_SCALARS_PER_POINT] = nb_scalars_per_point
        self.header[Field.NB_PROPERTIES_PER_STREAMLINE] = nb_properties_per_streamline

        # Overwrite header with updated one.
        self.file.seek(self.beginning, os.SEEK_SET)
        self.file.write(self.header[0].tostring())


class TrkFile(StreamlinesFile):
    ''' Convenience class to encapsulate TRK file format.

    Note
    ----
    TrackVis (so its file format: TRK) considers the streamline coordinate
    (0,0,0) to be in the corner of the voxel whereas NiBabel's streamlines
    internal representation (Voxel space) assume (0,0,0) to be in the
    center of the voxel.

    Thus, streamlines are shifted of half a voxel on load and are shifted
    back on save.
    '''

    # Contants
    MAGIC_NUMBER = b"TRACK"
    HEADER_SIZE = 1000

    @classmethod
    def get_magic_number(cls):
        ''' Return TRK's magic number. '''
        return cls.MAGIC_NUMBER

    @classmethod
    def can_save_scalars(cls):
        ''' Tells if the streamlines format supports saving scalars. '''
        return True

    @classmethod
    def can_save_properties(cls):
        ''' Tells if the streamlines format supports saving properties. '''
        return True

    @classmethod
    def is_correct_format(cls, fileobj):
        ''' Check if the file is in TRK format.

        Parameters
        ----------
        fileobj : string or file-like object
            If string, a filename; otherwise an open file-like object
            pointing to TRK file (and ready to read from the beginning
            of the TRK header data).

        Returns
        -------
        is_correct_format : boolean
            Returns True if `fileobj` is in TRK format.
        '''
        with Opener(fileobj) as f:
            magic_number = f.read(5)
            f.seek(-5, os.SEEK_CUR)
            return magic_number == cls.MAGIC_NUMBER

        return False

    @staticmethod
    def load(fileobj, ref=None, lazy_load=False):
        ''' Loads streamlines from a file-like object.

        Parameters
        ----------
        fileobj : string or file-like object
            If string, a filename; otherwise an open file-like object
            pointing to TRK file (and ready to read from the beginning
            of the TRK header).

        ref : filename | `Nifti1Image` object | 2D array (4,4) | None
            Reference space where streamlines live in `fileobj`.

        lazy_load : boolean (optional)
            Load streamlines in a lazy manner i.e. they will not be kept
            in memory.

        Returns
        -------
        streamlines : Streamlines object
            Returns an object containing streamlines' data and header
            information. See `nibabel.Streamlines`.

        Notes
        -----
        Streamlines are assumed to be in voxel space where coordinate (0,0,0)
        refers to the center of the voxel.
        '''
        trk_reader = TrkReader(fileobj)

        # Check if reference space matches one from TRK's header.
        affine = trk_reader.header[Field.to_world_space]
        if ref is not None:
            affine = get_affine_from_reference(ref)
            if not np.allclose(affine, trk_reader.header[Field.to_world_space]):
                raise ValueError("Reference space provided does not match the "
                                 " one from the TRK file header. Use `ref=None`"
                                 " to use one contained in the TRK file")

        #points      = lambda: (x[0] for x in trk_reader)
        #scalars     = lambda: (x[1] for x in trk_reader)
        #properties  = lambda: (x[2] for x in trk_reader)
        data = lambda: iter(trk_reader)

        if lazy_load:
            streamlines = LazyStreamlines.create_from_data(data)

            # Overwrite scalars and properties if there is none
            if trk_reader.header[Field.NB_SCALARS_PER_POINT] == 0:
                streamlines.scalars = lambda: []
            if trk_reader.header[Field.NB_PROPERTIES_PER_STREAMLINE] == 0:
                streamlines.properties = lambda: []
        else:
            streamlines = Streamlines(*zip(*data()))

            # Overwrite scalars and properties if there is none
            if trk_reader.header[Field.NB_SCALARS_PER_POINT] == 0:
                streamlines.scalars = []
            if trk_reader.header[Field.NB_PROPERTIES_PER_STREAMLINE] == 0:
                streamlines.properties = []

        # Set available common information about streamlines in the header
        streamlines.header.to_world_space = affine

        # If 'count' field is 0, i.e. not provided, we don't set `nb_streamlines`
        if trk_reader.header[Field.NB_STREAMLINES] > 0:
            streamlines.header.nb_streamlines = trk_reader.header[Field.NB_STREAMLINES]

        # Keep extra information about TRK format
        streamlines.header.extra = trk_reader.header

        ## Perform some integrity checks
        #if trk_reader.header[Field.VOXEL_ORDER] != streamlines.header.voxel_order:
        #    raise HeaderError("'voxel_order' does not match the affine.")
        #if streamlines.header.voxel_sizes != trk_reader.header[Field.VOXEL_SIZES]:
        #    raise HeaderError("'voxel_sizes' does not match the affine.")
        #if streamlines.header.nb_scalars_per_point != trk_reader.header[Field.NB_SCALARS_PER_POINT]:
        #    raise HeaderError("'nb_scalars_per_point' does not match.")
        #if streamlines.header.nb_properties_per_streamline != trk_reader.header[Field.NB_PROPERTIES_PER_STREAMLINE]:
        #    raise HeaderError("'nb_properties_per_streamline' does not match.")

        return streamlines

    @staticmethod
    def save(streamlines, fileobj, ref=None):
        ''' Saves streamlines to a file-like object.

        Parameters
        ----------
        streamlines : Streamlines object
            Object containing streamlines' data and header information.
            See 'nibabel.Streamlines'.

        fileobj : string or file-like object
            If string, a filename; otherwise an open file-like object
            pointing to TRK file (and ready to read from the beginning
            of the TRK header data).

        ref : filename | `Nifti1Image` object | 2D array (4,4) (optional)
            Reference space where streamlines will live in `fileobj`.

        Notes
        -----
        Streamlines are assumed to be in voxel space where coordinate (0,0,0)
        refers to the center of the voxel.
        '''
        if ref is not None:
            streamlines.header.to_world_space = get_affine_from_reference(ref)

        trk_writer = TrkWriter(fileobj, streamlines.header)
        trk_writer.write(streamlines)

    @staticmethod
    def pretty_print(fileobj):
        ''' Gets a formatted string of the header of a TRK file.

        Parameters
        ----------
        fileobj : string or file-like object
            If string, a filename; otherwise an open file-like object
            pointing to TRK file (and ready to read from the beginning
            of the header).

        Returns
        -------
        info : string
            Header information relevant to the TRK format.
        '''
        trk_reader = TrkReader(fileobj)
        hdr = trk_reader.header

        info = ""
        info += "MAGIC NUMBER: {0}".format(hdr[Field.MAGIC_NUMBER])
        info += "v.{0}".format(hdr['version'])
        info += "dim: {0}".format(hdr[Field.DIMENSIONS])
        info += "voxel_sizes: {0}".format(hdr[Field.VOXEL_SIZES])
        info += "orgin: {0}".format(hdr[Field.ORIGIN])
        info += "nb_scalars: {0}".format(hdr[Field.NB_SCALARS_PER_POINT])
        info += "scalar_name:\n {0}".format("\n".join(hdr['scalar_name']))
        info += "nb_properties: {0}".format(hdr[Field.NB_PROPERTIES_PER_STREAMLINE])
        info += "property_name:\n {0}".format("\n".join(hdr['property_name']))
        info += "vox_to_world: {0}".format(hdr[Field.to_world_space])
        info += "voxel_order: {0}".format(hdr[Field.VOXEL_ORDER])
        info += "image_orientation_patient: {0}".format(hdr['image_orientation_patient'])
        info += "pad1: {0}".format(hdr['pad1'])
        info += "pad2: {0}".format(hdr['pad2'])
        info += "invert_x: {0}".format(hdr['invert_x'])
        info += "invert_y: {0}".format(hdr['invert_y'])
        info += "invert_z: {0}".format(hdr['invert_z'])
        info += "swap_xy: {0}".format(hdr['swap_xy'])
        info += "swap_yz: {0}".format(hdr['swap_yz'])
        info += "swap_zx: {0}".format(hdr['swap_zx'])
        info += "n_count: {0}".format(hdr[Field.NB_STREAMLINES])
        info += "hdr_size: {0}".format(hdr['hdr_size'])
        info += "endianess: {0}".format(hdr[Field.ENDIAN])

        return info