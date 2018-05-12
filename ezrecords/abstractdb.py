# coding: utf-8
from __future__ import unicode_literals, print_function, absolute_import, with_statement
import os
import inspect
import codecs
from abc import ABCMeta, abstractmethod
from timeit import default_timer as timer
import warnings
import logging
from munch import Munch

from ezrecords.records import Record, RecordCollection
from ezrecords.util import (parse_db_url, format_timedelta, preg_replace,
                            str_replace, force_unicode)
from ezrecords.compat import numeric_types


class Database(object):
    """Database Access Helper.

    This is highly inspired by ezsql/wpdb from Justin Vincent and WordPress
    written in PHP, but by no means it is meant to have feature-parity with them.
    In fact the ideal thing would be having this merged with Records of Kenneth Reitz.

    Attributes:

    Notes:
        When using cursor or the connection directly to run SQL...

        Never do this. It's insecure and SQLInjectable
        >>> sql = "UPDATE people SET name='%s' WHERE id='%s'" % ('foo', 'bar')
        >>> cursor.execute(sql)

        Do this instead
        >>> name, id = 'foo', 'bar'
        >>> sql = "UPDATE people SET name=%s WHERE id=%s"
        >>> cursor.execute(sql, (name, id))  # notice the second param is a tuple
    """
    __metaclass__ = ABCMeta

    def __init__(self, db_url=None, logger=None):
        """Connects to the database server and selects a database."""
        # If no db_url was provided, fallback to $DATABASE_URL.
        self.db_url = db_url or os.getenv('DATABASE_URL', None)

        if not self.db_url:
            raise ValueError('You must provide a db_url.')

        dsn_components = parse_db_url(db_url)

        self._host = dsn_components['host']
        self._port = int(dsn_components['port'] or 0)
        self._user = dsn_components['username']
        self._password = dsn_components['password']
        self._database = dsn_components['database']

        self._collate = ''
        self._charset = ''

        # The logger used to log SQL statements when in debug and errors.
        self.logger = logger

        #: The current database transaction
        self._connection = None

        #: Flag indicating if current session is or not in transaction.
        self._in_transaction = False

        #: Flag indicating whether or not Error echoing is turned on.
        # Defaults to False.
        self.show_errors = False

        #: Flag indicating if all executed SQL code should be displayed.
        # Defaults to False.
        self.show_sql = False

        #: Flag indicating if queries and their stop times should be saved.
        #: If this is on, queries will be saved on saved_queries.
        self.save_queries = False

        #: List of all queries that were executed in this connection
        #: since last flush is `save_queries` is True
        self.saved_queries = []

        #: ID generated by AUTO_INCREMENT/SERIAL column in most recent INSERT.
        self.last_insert_id = 0

        #: The most recent query to have been executed.
        self.last_query = None

        #: The most recent error text generated by the database.
        self.last_error = ''

        #: The most recent query results.
        self._last_result = None

        #: The number of rows returned by the last query.
        self.affected_rows = 0

        #: The number of queries that have been executed
        self.queries_executed = 0

        #: The time the last query/current started
        self._time_start = None

        #: The time the last query stopped
        self._time_stop = None

        #: The placeholder used when preparing queries
        self._placeholder = '%s'

        # Establish database connection
        self.connect()

    @property
    def in_transaction(self):
        """Flag indicating if the current session is in a transaction."""
        return self._in_transaction

    def connect(self):
        """Establishes a database connection."""
        if self.show_sql and self.logger:
            self.logger.debug('host=%s port=%s user=%s password=%s database=%s' %
                              (self._host, self._port, self._user, '***', self._database))

        self._connect()

    @abstractmethod
    def _connect(self):
        raise NotImplementedError()

    def set_charset(self, charset, collate=None):
        """Sets the connection's character set.

        Args:
            charset (str): The character set
            collate (str, optional): The collation
        """
        if self.show_sql and self.logger:
            self.logger.debug('Setting conn charset: %s, collate: %s' % (charset, collate))

        self._set_charset(charset, collate)

        self._charset = charset
        self._collate = collate

    @abstractmethod
    def _set_charset(self, charset, collate=None):
        raise NotImplementedError()

    def close(self):
        """Closes the current database connection."""
        if self._connection is None:
            raise RuntimeError('Cannot close connection, DB is not bound to any.')
        self._connection.close()

    def get_connection(self):
        """Gets the current database connection."""
        self.connect()
        return self._connection

    def get_cursor(self):
        """Gets a new cursor from the current connection."""
        self.connect()
        return self._connection.cursor()

    def prepare(self, sql, *args):
        """Prepares a SQL query for safe execution.

        Only the `%s` directive is accepted in the query format string,
        but if any of the following is given it will be replaced by the
        former:
            %d (integer)
            %f (float)

        Args:
            sql (str): Query statement with sprintf()-like placeholders.
            args (mixed): The variables to substitute into the query's placeholders.

        Returns:
            str: Sanitized query string, if there is a query to prepare.
        """
        if sql is None:
            return

        self.connect()
        cursor = self._connection.cursor()

        sql = str_replace("'%s'", self._placeholder, sql)  # single-quote unquoting
        sql = str_replace('"%s"', self._placeholder, sql)  # double-quote unquoting
        sql = str_replace('%f', self._placeholder, sql)  # %f to %s
        sql = str_replace('%d', self._placeholder, sql)  # %f to %s
        sql = preg_replace(r'(%)\1+', r'\1', sql)  # quote the strings, avoiding escaped strings like %%s

        args = map(lambda x: x if isinstance(x, numeric_types) else (x), args)

        args = tuple(args)

        if len(args) == 0:
            return sql

        # mogrify is not standard cursor method
        clean_sql = cursor.mogrify(sql, tuple(args)) if hasattr(cursor, 'mogrify') else sql
        return clean_sql

    def query(self, sql, *args, **kwargs):
        """Perform a database query, using current database connection.

        Args:
            sql (str): the SQL query
            *args: Values to be replace into the format string
            **kwargs:
                one=True indicates that only one result should be returned
                proc=True indicates that the query is a stored procedure name

        Returns:
            A `RecordCollection`, which can be iterated over to get result rows
            as dictionaries, or as single `Record` if `one=True` is passed as a
            kwarg.

        Examples:
            >>> user = db.query('SELECT * FROM users WHERE id = %s', 1, one=True)

            >>> user = db.query('SELECT * FROM users WHERE name = %s', name)

            >>> db.query('sum_values', 1, 2, proc=True)
            3

        TODO:
            * detect cases of multi queries and warn about them. Since not every
              driver supports
        """
        self.connect()
        cursor = self._connection.cursor()

        proc = kwargs.get('proc', False)
        one = kwargs.get('one', False)

        if proc:
            cursor.callproc(sql, args)
            self.last_query = sql + ', '.join(map(lambda x: str(x), args))
        else:
            sql = self.prepare(sql)
            # NOTE: mogrify is not a standard cursor method in PEP 249
            self.last_query = cursor.mogrify(sql, args) if hasattr(cursor, 'mogrify') else sql
            if self.save_queries:
                self.timer_start()

            cursor.execute(sql, args)
            if self.save_queries:
                elapsed_time = self.timer_stop()
                caller = inspect.stack()[1]
                query_to_save = (self.last_query, elapsed_time, 'file %s, function %s' % (caller[1], caller[3]))
                self.saved_queries.append(query_to_save)

            self.queries_executed += 1

        self.affected_rows = cursor.rowcount
        self.last_insert_id = cursor.lastrowid

        if self.show_sql and self.logger:
            self.logger.debug('last_query: %s' % force_unicode(self.last_query))

        rv = None
        try:
            rv = cursor.fetchall()
        except exception:
            if self.logger: logger.exception(exception)

        cursor.close()

        if rv is None:
            return

        # Row-by-row Record generator.
        row_gen = (Record(list(row.keys()), list(row.values())) for row in rv)

        # Convert psycopg2 results to RecordCollection.
        results = RecordCollection(row_gen)

        if one:
            self._last_result = results.first()
        else:
            self._last_result = results

        return self._last_result

    def query_one(self, sql, *args):
        """Perform a database query and returns the first result or None"""
        try:
            rv = self.query(sql, *args, one=True)
        except IndexError:
            return None
        return rv

    def query_file(self, path, **kwargs):
        """Runs a query from the given filename"""

        if not os.path.exists(path):
            raise IOError("File '{}' not found!".format(path))

        if os.path.isdir(path):
            raise IOError("'{}' is a directory!".format(path))

        with codecs.open(path, 'r', 'utf-8') as file_handle:
            query = file_handle.read()

        return self.query(query, **kwargs)

    def call_procedure(self, procedure, *args):
        """Runs a stored procedure.

        Args:
            procedure (str): the procedure name
            *args: the positional procedure parameters in respective order.

        Returns:

        Notes:
            When creating procedures don't use DELIMITER. It's mysql
            command-line command not a SQL command and won't work.

        """
        return self.query(procedure, *args, proc=True)

    # ------------------------------------------------------------------
    # DML
    # ------------------------------------------------------------------

    def get_var(self, query, column_offset=0, row_offset=0):
        """Retrieve one variable from the database.

        Executes a SQL query and returns the value from the SQL result.

        Args:
            query (str): SQL query
            column_offset (int, optional): Column of value to return.
                Defaults to 0
            row_offset (int, optional): Row of value to return. Indexed from 0.
                Defaults to 0

        Returns:
            Database query result (as string)

        Examples:
            >>> db.get_var('SELECT version()')
            5.7.15
        """
        rows = self.query(query)

        return rows[row_offset][column_offset]

    def get_row(self, query, output_type='record', row_offset=0):
        """Retrieve one row from the database.

        Executes a SQL query and returns the row from the SQL result.

        Args:
            query (str): SQL query
            output_type (str, optional): The required return type.
                One of 'record', 'dict', 'dataset', 'object.'
            row_offset (int, optional): Row to return. Indexed from 0.
                Defaults to 0.

        Returns:
            Database query result in format specified by `output_type`

        Examples:
            Get the second row from the first 10 users
            >>> db.get_row('SELECT * FROM users LIMIT 10', 'object', 1)
        """
        rows = self.query(query)
        row = rows[row_offset]

        if output_type == 'record':
            return row
        elif output_type == 'dict':
            return row.as_dict()
        elif output_type == 'dataset':
            return row.dataset
        elif output_type == 'object':
            return Munch.fromDict(row.as_dict)

        return None

    def get_col(self, query, column_offset=0):
        """Retrieve one column from the database.
        Executes a SQL query and returns the column from the SQL result.

        Args:
            query(str): SQL query
            column_offset(int, optional): Column to return. Indexed from 0

        Returns:
            List indexed from 0 by SQL result row number.

        Examples:
            Get the user mails of all moderators
            >>> db.get_col("SELECT id, username, email FROM users WHERE role='moderator'", 2)
        """
        rows = self.query(query)
        column = list(map(lambda x: x[column_offset], rows))

        return column

    def get_results(self, query, output_type='record'):
        """Retrieve an entire SQL result set from the database (i.e., many rows)

        Executes a SQL query and returns the entire SQL result.

        Args:
            query (str): SQL query
            output_type(str, optional): The required output type for the records.
                One of 'record', 'dict', 'dataset', 'object.'

        Returns:
            list: Database query results with values of the indicated `output_type`

        Examples:
            >>> db.get_results('SELECT * FROM users', 'object')
        """
        rows = self.query(query)
        output_rows = []

        for row in rows:
            if output_type == 'record':
                output_rows.append(row)
            elif output_type == 'dict':
                output_rows.append(row.as_dict())
            elif output_type == 'dataset':
                output_rows.append(row.dataset)
            elif output_type == 'object':
                output_rows.append(Munch.fromDict(row.as_dict))

        return output_rows

    def insert(self, table, data=None, **kwargs):
        """Inserts a single row into a table.

        Args:
            table (str): Table name
            data (dict): Data to insert in column, value pairs
                Sending a None value will cause the column to be set to NULL
            **kwargs: Arbitrary column, value pairs of data to insert as
                keyword arguments

        Returns:
            int: The number of rows inserted (1) or -1 on error.

        Examples:
            >>> db.insert('table', column = 'value')
            >>> db.insert('table', { column : 'value' })

        """
        if data is not None:
            kwargs.update(data)

        values = kwargs.values()

        sql = 'INSERT INTO %s (%s) VALUES (%s)' % (
            table,
            ', '.join(kwargs.keys()),
            ', '.join([self._placeholder] * len(values))
        )

        self.query(sql, *values)

        return self.affected_rows

    def bulk_insert(self, table, columns, values):
        """Bulk insert

        Args:
            table (str): Table name
            columns (tuple|list): columns to insert
            values (tuple|list): values to insert

        Returns:
            int: The number of rows inserted (1) or -1 on error.

        Examples:
            >>> db.bulk_insert('table', (column, column2), [(value1, value2), (value3, value4)])
            >>> db.bulk_insert('table', [column, column2], [(value1, value2), (value1, value2)])
        """

        single_values = '(' + ', '.join([self._placeholder] * len(columns)) + ')'

        sql = 'INSERT INTO %s (%s) VALUES %s' % (
            table,
            ', '.join(columns),
            ', '.join([single_values] * len(values))
        )

        all_values = []
        for value in values:
            all_values.extend(list(value))

        self.query(sql, *tuple(all_values))

        return self.affected_rows

    def delete(self, table, where=None, **kwargs):
        """Deletes rows in the table.

        Args:
            table (str): Table name
            where (dict): Dictionary of WHERE clauses in column, value pairs
                Multiple clauses will be joined with ANDs.
                Sending a None value will create an IS NULL comparison.
            **kwargs: Arbitrary column, value pairs to use as WHERE clauses

        Returns:
            int: The number of rows deleted, or -1 on error.

        Examples:
            >>> db.delete('table')
            >>> db.delete('table', column = 'value')
            >>> db.delete('table', {'column': 'value'})

        """
        if where is not None:
            kwargs.update(where)

        conditions, values = [], []
        for field, value in kwargs.items():
            if value is None:
                conditions.append('"%s" IS NULL' % field)
                continue

            conditions.append('"' + field + '" = ' + self._placeholder)
            values.append(value)

        conditions = ' AND'.join(conditions)

        sql = 'DELETE FROM "%s" ' % table
        if conditions:
            where_clause = ' WHERE %s' % conditions
            sql += where_clause

        self.query(sql, *values)

        return self.affected_rows

    def update(self, table, data, where):
        """Update rows in the table.

        Args:
            table (str): Table name
            data (dict): Data to insert in column, value pairs
                Sending a None value will cause the column to be set to NULL
            where (dict): Dictionary of WHERE clauses in column, value pairs
                Multiple clauses will be joined with ANDs.
                Sending a None value will create an IS NULL comparison.

        Returns:
            int: The number of rows updated, or -1 on error.

        Examples:
            >>> db.update('table', {'column': 'new value'}, {'column': 'old value'})

        """
        if not isinstance(data, dict) or not isinstance(where, dict):
            return False

        fields, conditions, values = [], [], []

        for field, value in data.items():
            if value is None:
                fields.append('"%s" = NULL' % field)
                continue

            fields.append('"' + field + '" = ' + self._placeholder)
            values.append(value)

        for field, value in where.items():
            if value is None:
                conditions.append('"%s" IS NULL' % field)
                continue

            conditions.append('"' + field + '" = ' + self._placeholder)
            values.append(value)

        fields = ', '.join(fields)
        conditions = ' AND '.join(conditions)

        sql = 'UPDATE "%s" SET %s WHERE %s' % (table, fields, conditions)

        self.query(sql, *values)

        return self.affected_rows

    # ------------------------------------------------------------------
    # Transaction Management
    # ------------------------------------------------------------------

    def begin_transaction(self):
        """Begins a transaction on the current connection.

        Raises:
            RuntimeError: If there's no current connection.
        """
        if self._connection is None:
            raise RuntimeError('Cannot BEGIN/START TRANSACTION on no connection. Connect first.')

        self._connection.begin()
        self._in_transaction = True

    def rollback(self):
        """Rollback the current transaction on the current connection.

        Raises:
            RuntimeError: If there's no current connection.
        """
        if self._connection is None or not self._in_transaction:
            raise RuntimeError("Cannot ROLLBACK. There's no current connection")

        self._connection.rollback()
        self._in_transaction = False

    def commit(self):
        """Commits the current transaction on the current connection.

        Raises:
            RuntimeError: If there's no current connection.
        """
        if self._connection is None:
            raise RuntimeError("Cannot COMMIT. There's no current connection.")

        self._connection.commit()
        self._in_transaction = False

    # ------------------------------------------------------------------
    # Helpers & Common queries
    # Made most of them non-abstract so we don't force anyone to implement
    # ------------------------------------------------------------------

    def db_version(self):
        """Retrieves the database server version number."""
        version = self._db_version()
        version = preg_replace(r'[^0-9.].*', '', version)
        return version

    @abstractmethod
    def _db_version(self):
        """Retrieves the database server version."""
        raise NotImplementedError()

    def use(self, db_name):
        """Selects a database using the current database connection.

        The database name will be changed based on the current database
        connection. On failure, the execution will bail and display an DB error

        Args:
            db_name (str): database name
        """
        raise NotImplementedError()

    def exists(self, name, kind='table', schema='public'):
        """Checks if an object with the given name, type exists in the given schema.

        Args:
            name (str): the object name to be checked
            kind (str, optional): the type of the object
            schema (str, optional): the schema in which the object belongs

        Returns:
            bool: True case the object exists, False otherwise.
        """
        raise NotImplementedError()

    def get_table_names(self):
        """Returns a list of table names for the connected database."""
        return self._get_table_names()

    def _get_table_names(self):
        """Retrieves the database server version."""
        raise NotImplementedError()

    # ------------------------------------------------------------------
    # Debug Helpers
    # ------------------------------------------------------------------

    def flush(self):
        """Cache bust of results"""
        self.last_error = ''
        self.affected_rows = 0
        self.last_query = 0
        self.queries_executed = 0
        del self.saved_queries[:]

    def timer_start(self):
        """Starts the timer, for debugging purposes."""
        self._time_start = timer()

    def timer_stop(self):
        """Stops the debugging timer.

        Returns the elapsed time sinice last `timer_start()` call
        """
        self._time_stop = timer()
        return format_timedelta(self._time_stop - self._time_start)

    @property
    def last_query_elapsed_time(self):
        """Returns the amount of elapsed time during the most recent query."""
        return format_timedelta(self._time_stop - self._time_start)
