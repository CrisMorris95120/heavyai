import heavydb

import base64
import pandas as pd
import pyarrow as pa
import ctypes

from heavydb.thrift.Heavy import TCreateParams
from heavydb.common.ttypes import TDeviceType
from heavydb.thrift.ttypes import (
    TDashboard,
    TArrowTransport,
)

from heavydb._parsers import _bind_parameters, _extract_column_details
from ._utils import _parse_tdf_gpu

from ._loaders import _build_input_rows
from ._transforms import _change_dashboard_sources
from .ipc import load_buffer, shmdt
from ._pandas_loaders import build_row_desc, _serialize_arrow_payload
from . import _pandas_loaders
from ._mutators import set_tdf, get_tdf
from types import MethodType


class Connection(heavydb.Connection):
    def create_table(self, table_name, data, preserve_index=False):
        """Create a table from a pandas.DataFrame

        Parameters
        ----------
        table_name: str
        data: DataFrame
        preserve_index: bool, default False
            Whether to create a column in the table for the DataFrame index
        """

        row_desc = build_row_desc(data, preserve_index=preserve_index)
        self._client.create_table(
            self._session,
            table_name,
            row_desc,
            TCreateParams(False),
        )

    def load_table(
        self,
        table_name,
        data,
        method='infer',
        preserve_index=False,
        create='infer',
        column_names=list(),
    ):
        """Load data into a table

        Parameters
        ----------
        table_name: str
        data: pyarrow.Table, pandas.DataFrame, or iterable of tuples
        method: {'infer', 'columnar', 'rows', 'arrow'}
            Method to use for loading the data. Three options are available

            1. ``pyarrow`` and Apache Arrow loader
            2. columnar loader
            3. row-wise loader

            The Arrow loader is typically the fastest, followed by the
            columnar loader, followed by the row-wise loader. If a DataFrame
            or ``pyarrow.Table`` is passed and ``pyarrow`` is installed, the
            Arrow-based loader will be used. If arrow isn't available, the
            columnar loader is used. Finally, ``data`` is an iterable of tuples
            the row-wise loader is used.

        preserve_index: bool, default False
            Whether to keep the index when loading a pandas DataFrame

        create: {"infer", True, False}
            Whether to issue a CREATE TABLE before inserting the data.

            * infer: check to see if the table already exists, and create
              a table if it does not
            * True: attempt to create the table, without checking if it exists
            * False: do not attempt to create the table

        See Also
        --------
        load_table_arrow
        load_table_columnar
        """

        if create not in ['infer', True, False]:
            raise ValueError(
                f"Unexpected value for create: '{create}'. "
                "Expected one of {'infer', True, False}"
            )

        if create == 'infer':
            # ask the database if we already exist, creating if not
            create = table_name not in set(
                self._client.get_tables(self._session)
            )

        if create:
            self.create_table(table_name, data)

        if method == 'infer':
            if (
                isinstance(data, pd.DataFrame)
                or isinstance(data, pa.Table)
                or isinstance(data, pa.RecordBatch)
            ):  # noqa
                return self.load_table_arrow(table_name, data)

            elif isinstance(data, pd.DataFrame):
                return self.load_table_columnar(table_name, data)

        elif method == 'arrow':
            return self.load_table_arrow(table_name, data)

        elif method == 'columnar':
            return self.load_table_columnar(table_name, data)

        elif method != 'rows':
            raise TypeError(
                "Method must be one of {{'infer', 'arrow', "
                "'columnar', 'rows'}}. Got {} instead".format(method)
            )

        if isinstance(data, pd.DataFrame):
            # We need to convert a Pandas dataframe to a list of tuples before
            # loading row wise
            data = data.itertuples(index=preserve_index, name=None)

        input_data = _build_input_rows(data)
        self._client.load_table(
            self._session, table_name, input_data, column_names
        )

    def load_table_rowwise(self, table_name, data, column_names=list()):
        """Load data into a table row-wise

        Parameters
        ----------
        table_name: str
        data: Iterable of tuples
            Each element of `data` should be a row to be inserted

        See Also
        --------
        load_table
        load_table_arrow
        load_table_columnar

        Examples
        --------
        >>> data = [(1, 'a'), (2, 'b'), (3, 'c')]
        >>> con.load_table('bar', data)
        """
        input_data = _build_input_rows(data)
        self._client.load_table(
            self._session, table_name, input_data, column_names
        )

    def load_table_columnar(
        self,
        table_name,
        data,
        preserve_index=False,
        chunk_size_bytes=0,
        col_names_from_schema=False,
        column_names=list(),
    ):
        """Load a pandas DataFrame to the database using HeavyDB's Thrift-based
        columnar format

        Parameters
        ----------
        table_name: str
        data: DataFrame
        preserve_index: bool, default False
            Whether to include the index of a pandas DataFrame when writing.
        chunk_size_bytes: integer, default 0
            Chunk the loading of columns to prevent large Thrift requests. A
            value of 0 means do not chunk and send the dataframe as a single
            request
        col_names_from_schema: bool, default False
            Read the existing table schema to determine the column names. This
            will read the schema of an existing table in HeavyDB and match
            those names to the column names of the dataframe. This is for
            user convenience when loading from data that is unordered,
            especially handy when a table has a large number of columns.

        Examples
        --------
        >>> df = pd.DataFrame({"a": [1, 2, 3], "b": ['d', 'e', 'f']})
        >>> con.load_table_columnar('foo', df, preserve_index=False)

        See Also
        --------
        load_table
        load_table_arrow
        load_table_rowwise

        Notes
        -----
        Use ``pymapd >= 0.11.0`` while running with ``heavydb >= 4.6.0`` in
        order to avoid loading inconsistent values into DATE column.
        """

        if not isinstance(data, pd.DataFrame):
            raise TypeError('Unknown type {}'.format(type(data)))

        table_details = self.get_table_details(table_name)
        # Validate that there are the same number of columns in the table
        # as there are in the dataframe. No point trying to load the data
        # if this is not the case
        if len(table_details) != len(data.columns):
            raise ValueError(
                'Number of columns in dataframe ({}) does not \
                                match number of columns in HeavyDB table \
                                ({})'.format(
                    len(data.columns), len(table_details)
                )
            )

        col_names = (
            [i.name for i in table_details]
            if col_names_from_schema
            else list(data)
        )

        col_types = table_details

        input_cols = _pandas_loaders.build_input_columnar(
            data,
            preserve_index=preserve_index,
            chunk_size_bytes=chunk_size_bytes,
            col_types=col_types,
            col_names=col_names,
        )

        for cols in input_cols:
            self._client.load_table_binary_columnar(
                self._session, table_name, cols, column_names
            )

    def load_table_arrow(
        self, table_name, data, preserve_index=False, load_column_names=list()
    ):
        """Load a pandas.DataFrame or a pyarrow Table or RecordBatch to the
        database using Arrow columnar format for interchange

        Parameters
        ----------
        table_name: str
        data: pandas.DataFrame, pyarrow.RecordBatch, pyarrow.Table
        preserve_index: bool, default False
            Whether to include the index of a pandas DataFrame when writing.

        Examples
        --------
        >>> df = pd.DataFrame({"a": [1, 2, 3], "b": ['d', 'e', 'f']})
        >>> con.load_table_arrow('foo', df, preserve_index=False)

        See Also
        --------
        load_table
        load_table_columnar
        load_table_rowwise
        """
        metadata = self.get_table_details(table_name)
        payload = _serialize_arrow_payload(
            data, metadata, preserve_index=preserve_index
        )
        self._client.load_table_binary_arrow(
            self._session, table_name, payload.to_pybytes(), load_column_names
        )

    def select_ipc_gpu(
        self,
        operation,
        parameters=None,
        device_id=0,
        first_n=-1,
        release_memory=True,
    ):
        """Execute a ``SELECT`` operation using GPU memory.

        Parameters
        ----------
        operation: str
            A SQL statement
        parameters: dict, optional
            Parameters to insert into a parametrized query
        device_id: int
            GPU to return results to
        first_n: int, optional
            Number of records to return
        release_memory: bool, optional
            Call ``self.deallocate_ipc_gpu(df)`` after DataFrame created

        Returns
        -------
        gdf: cudf.GpuDataFrame

        Notes
        -----
        This method requires ``cudf`` and ``libcudf`` to be installed.
        An ``ImportError`` is raised if those aren't available.

        This method requires the Python code to be executed on the same machine
        where HeavyDB running.
        """
        try:
            from cudf.comm.gpuarrow import GpuArrowReader  # noqa
            from cudf.core.dataframe import DataFrame  # noqa
        except ImportError:
            raise ImportError(
                "The 'cudf' package is required for `select_ipc_gpu`"
            )

        self.register_runtime_udfs()
        if parameters is not None:
            operation = str(_bind_parameters(operation, parameters))

        tdf = self._client.sql_execute_gdf(
            self._session,
            operation.strip(),
            device_id=device_id,
            first_n=first_n,
        )
        self._tdf = tdf

        df = _parse_tdf_gpu(tdf)

        # Deallocate TDataFrame at HeavyDB instance
        if release_memory:
            self.deallocate_ipc_gpu(df)

        return df

    def select_ipc(
        self,
        operation,
        parameters=None,
        first_n=-1,
        release_memory=True,
        transport_method=TArrowTransport.WIRE,
    ):
        """Execute a ``SELECT`` operation using CPU shared memory

        Parameters
        ----------
        operation: str
            A SQL select statement
        parameters: dict, optional
            Parameters to insert for a parametrized query
        first_n: int, optional
            Number of records to return
        release_memory: bool, optional
            Call ``self.deallocate_ipc(df)`` after DataFrame created

        Returns
        -------
        df: pandas.DataFrame

        Notes
        -----
        This method requires the Python code to be executed on the same machine
        where HeavyDB running.
        """
        self.register_runtime_udfs()

        if parameters is not None:
            operation = str(_bind_parameters(operation, parameters))

        tdf = self._client.sql_execute_df(
            self._session,
            operation.strip(),
            device_type=0,
            device_id=0,
            first_n=first_n,
            transport_method=transport_method,
        )
        self._tdf = tdf

        if transport_method == TArrowTransport.WIRE:
            reader = pa.ipc.open_stream(tdf.df_buffer)
            tbl = reader.read_all()
            df = tbl.to_pandas()
            return df

        elif transport_method == TArrowTransport.SHARED_MEMORY:
            df_buf = load_buffer(tdf.df_handle, tdf.df_size)
            reader = pa.ipc.open_stream(df_buf[0])
            tbl = reader.read_all()
            df = tbl.to_pandas()

            # this is needed to modify the df object for deallocate_df to work
            df.set_tdf = MethodType(set_tdf, df)
            df.get_tdf = MethodType(get_tdf, df)

            # Because deallocate_df can be called any time in future, keep tdf
            # from HeavyDB so that it can be used whenever
            # deallocate_df called
            df.set_tdf(tdf)

            # free shared memory from Python
            # https://github.com/omnisci/pymapd/issues/46
            # https://github.com/omnisci/pymapd/issues/31
            free_df = shmdt(ctypes.cast(df_buf[1], ctypes.c_void_p))  # noqa

            # Deallocate TDataFrame at HeavyDB instance
            if release_memory:
                self.deallocate_ipc(df)

            return df
        else:
            raise RuntimeError(
                "The specified transport type is not supported."
                " Only SHARED_MEMORY and WIRE are supported."
            )

    def deallocate_ipc_gpu(self, df, device_id=0):
        """Deallocate a DataFrame using GPU memory.

        Parameters
        ----------
        device_ids: int
            GPU which contains TDataFrame
        """

        tdf = df.get_tdf()
        result = self._client.deallocate_df(
            session=self._session,
            df=tdf,
            device_type=TDeviceType.GPU,
            device_id=device_id,
        )
        return result

    def deallocate_ipc(self, df, device_id=0):
        """Deallocate a DataFrame using CPU shared memory.

        Parameters
        ----------
        device_id: int
            GPU which contains TDataFrame
        """
        tdf = df.get_tdf()
        result = self._client.deallocate_df(
            session=self._session,
            df=tdf,
            device_type=TDeviceType.CPU,
            device_id=device_id,
        )
        return result

    # --------------------------------------------------------------------------
    # Convenience methods
    # --------------------------------------------------------------------------
    def get_tables(self):
        """List all the tables in the database

        Examples
        --------
        >>> con.get_tables()
        ['flights_2008_10k', 'stocks']
        """
        return self._client.get_tables(self._session)

    def get_table_details(self, table_name):
        """Get the column names and data types associated with a table.

        Parameters
        ----------
        table_name: str

        Returns
        -------
        details: List[tuples]

        Examples
        --------
        >>> con.get_table_details('stocks')
        [ColumnDetails(name='date_', type='STR', nullable=True, precision=0,
                       scale=0, comp_param=32, encoding='DICT'),
         ColumnDetails(name='trans', type='STR', nullable=True, precision=0,
                       scale=0, comp_param=32, encoding='DICT'),
         ...
        ]
        """
        details = self._client.get_table_details(self._session, table_name)
        return _extract_column_details(details.row_desc)

    def get_dashboard(self, dashboard_id):
        """Return the dashboard object of a specific dashboard

        Examples
        --------
        >>> con.get_dashboard(123)
        """
        dashboard = self._client.get_dashboard(
            session=self._session, dashboard_id=dashboard_id
        )
        return dashboard

    def get_dashboards(self):
        """List all the dashboards in the database

        Examples
        --------
        >>> con.get_dashboards()
        """
        dashboards = self._client.get_dashboards(session=self._session)
        return dashboards

    def create_dashboard(self, dashboard: TDashboard) -> int:
        """Create a new dashboard

        Parameters
        ----------

            dashboard: TDashboard
                The HeavyDB dashboard object to create

        Returns
        -------
            dashboardid: int
                The dashboard id of the new dashboard
        """
        return self._client.create_dashboard(
            session=self._session,
            dashboard_name=dashboard.dashboard_name,
            dashboard_state=dashboard.dashboard_state,
            image_hash=dashboard.image_hash,
            dashboard_metadata=dashboard.dashboard_metadata,
        )

    def change_dashboard_sources(
        self, dashboard: TDashboard, remap: dict
    ) -> TDashboard:
        """Change the sources of a dashboard

        Parameters
        ----------

        dashboard: TDashboard
            The HeavyDB dashboard object to transform
        remap: dict
            EXPERIMENTAL
            A dictionary remapping table names. The old table name(s)
            should be keys of the dict, with each value being another
            dict with a 'name' key holding the new table value. This
            structure can be used later to support changing column
            names.

        Returns
        -------
        dashboard: TDashboard
            An HeavyDB dashboard with the sources remapped

        Examples
        --------
        >>> source_remap = {'oldtablename1': {'name': 'newtablename1'}, \
'oldtablename2': {'name': 'newtablename2'}}
        >>> dash = con.get_dashboard(1)
        >>> newdash = con.change_dashboard_sources(dash, source_remap)

        See Also
        --------
        duplicate_dashboard
        """
        return _change_dashboard_sources(dashboard, remap)

    def duplicate_dashboard(
        self, dashboard_id, new_name=None, source_remap=None
    ):
        """
        Duplicate an existing dashboard, returning the new dashboard id.

        Parameters
        ----------

        dashboard_id: int
            The id of the dashboard to duplicate
        new_name: str
            The name for the new dashboard
        source_remap: dict
            EXPERIMENTAL
            A dictionary remapping table names. The old table name(s)
            should be keys of the dict, with each value being another
            dict with a 'name' key holding the new table value. This
            structure can be used later to support changing column
            names.

        Examples
        --------
        >>> source_remap = {'oldtablename1': {'name': 'newtablename1'}, \
'oldtablename2': {'name': 'newtablename2'}}
        >>> newdash = con.duplicate_dashboard(12345, "new dash", source_remap)
        """
        source_remap = source_remap or {}
        d = self._client.get_dashboard(
            session=self._session, dashboard_id=dashboard_id
        )

        newdashname = new_name or '{0} (Copy)'.format(d.dashboard_name)
        d = (
            self.change_dashboard_sources(d, source_remap)
            if source_remap
            else d
        )
        d.dashboard_name = newdashname
        return self.create_dashboard(d)

    def render_vega(self, vega, compression_level=1):
        """Render vega data on the database backend,
        returning the image as a PNG.

        Parameters
        ----------

        vega: dict
            The vega specification to render.
        compression_level: int
            The level of compression for the rendered PNG. Ranges from
            0 (low compression, faster) to 9 (high compression, slower).
        """
        result = self._client.render_vega(
            self._session,
            widget_id=None,
            vega_json=vega,
            compression_level=compression_level,
            nonce=None,
        )
        rendered_vega = RenderedVega(result)
        return rendered_vega


class RenderedVega:
    def __init__(self, render_result):
        self._render_result = render_result
        self.image_data = base64.b64encode(render_result.image).decode()

    def _repr_mimebundle_(self, include=None, exclude=None):
        return {
            'image/png': self.image_data,
            'text/html': (
                '<img src="data:image/png;base64,{}" '
                'alt="HeavyAI Vega">'.format(self.image_data)
            ),
        }


def connect(
    uri=None,
    user=None,
    password=None,
    host=None,
    port=6274,
    dbname=None,
    protocol='binary',
    sessionid=None,
    bin_cert_validate=None,
    bin_ca_certs=None,
    idpurl=None,
    idpformusernamefield='username',
    idpformpasswordfield='password',
    idpsslverify=True,
):
    """
    Create a new Connection.

    Parameters
    ----------
    uri: str
    user: str
    password: str
    host: str
    port: int
    dbname: str
    protocol: {'binary', 'http', 'https'}
    sessionid: str
    bin_cert_validate: bool, optional, binary encrypted connection only
        Whether to continue if there is any certificate error
    bin_ca_certs: str, optional, binary encrypted connection only
        Path to the CA certificate file
    idpurl : str
        EXPERIMENTAL Enable SAML authentication by providing
        the logon page of the SAML Identity Provider.
    idpformusernamefield: str
        The HTML form ID for the username, defaults to 'username'.
    idpformpasswordfield: str
        The HTML form ID for the password, defaults to 'password'.
    idpsslverify: str
        Enable / disable certificate checking, defaults to True.

    Returns
    -------
    conn: Connection

    Examples
    --------
    You can either pass a string ``uri``, all the individual components,
    or an existing sessionid excluding user, password, and database

    >>> connect('heavydb://admin:HyperInteractive@localhost:6274/heavyai?'
    ...         'protocol=binary')
    Connection(mapd://heavydb:***@localhost:6274/heavyai?protocol=binary)

    >>> connect(user='admin', password='HyperInteractive', host='localhost',
    ...         port=6274, dbname='heavyai')

    >>> connect(user='admin', password='HyperInteractive', host='localhost',
    ...         port=443, idpurl='https://sso.localhost/logon',
    ...         protocol='https')

    >>> connect(sessionid='XihlkjhdasfsadSDoasdllMweieisdpo', host='localhost',
    ...         port=6273, protocol='http')

    """
    return Connection(
        uri=uri,
        user=user,
        password=password,
        host=host,
        port=port,
        dbname=dbname,
        protocol=protocol,
        sessionid=sessionid,
        bin_cert_validate=bin_cert_validate,
        bin_ca_certs=bin_ca_certs,
        idpurl=idpurl,
        idpformusernamefield=idpformusernamefield,
        idpformpasswordfield=idpformpasswordfield,
        idpsslverify=idpsslverify,
    )
