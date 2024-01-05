import functools
from typing import Callable

import numpy as np
import pandas as pd
import toolz
from dask.dataframe import hyperloglog, methods
from dask.dataframe._compat import PANDAS_GE_200
from dask.dataframe.core import (
    _concat,
    idxmaxmin_agg,
    idxmaxmin_chunk,
    idxmaxmin_combine,
    is_dataframe_like,
    is_index_like,
    is_series_like,
    make_meta,
    meta_nonempty,
    total_mem_usage,
)
from dask.utils import M, apply, funcname

from dask_expr._concat import Concat
from dask_expr._expr import (
    Blockwise,
    Expr,
    Index,
    Projection,
    RenameFrame,
    RenameSeries,
    ResetIndex,
    ToFrame,
    determine_column_projection,
    plain_column_projection,
)
from dask_expr._util import _tokenize_deterministic, is_scalar


class Chunk(Blockwise):
    """Partition-wise component of `ApplyConcatApply`

    This class is used within `ApplyConcatApply._lower`.

    See Also
    --------
    ApplyConcatApply
    """

    _parameters = ["frame", "kind", "chunk", "chunk_kwargs"]

    @property
    def operation(self):
        return self.chunk

    @functools.cached_property
    def _args(self) -> list:
        return [self.frame]

    @functools.cached_property
    def _kwargs(self) -> dict:
        return self.chunk_kwargs or {}

    def _tree_repr_lines(self, indent=0, recursive=True):
        header = f"{funcname(self.kind)}({funcname(type(self))}):"
        lines = []
        if recursive:
            for dep in self.dependencies():
                lines.extend(dep._tree_repr_lines(2))

        for k, v in self._kwargs.items():
            try:
                if v != self.kind._defaults[k]:
                    header += f" {k}={v}"
            except KeyError:
                header += f" {k}={v}"

        lines = [header] + lines
        lines = [" " * indent + line for line in lines]
        return lines


class Aggregate(Chunk):
    """Partition-wise aggregation component of `ApplyConcatApply`

    This class is used within `ApplyConcatApply._lower`.
    See Also
    --------
    ApplyConcatApply
    """

    _parameters = ["frame", "kind", "aggregate", "aggregate_kwargs"]

    @staticmethod
    def _call_with_list_arg(func, *args, **kwargs):
        return func(list(args), **kwargs)

    @property
    def operation(self):
        return functools.partial(self._call_with_list_arg, self.aggregate)

    @functools.cached_property
    def _args(self) -> list:
        return [self.frame]

    @functools.cached_property
    def _kwargs(self) -> dict:
        return self.aggregate_kwargs or {}


class ShuffleReduce(Expr):
    """Shuffle-reduction component of `ApplyConcatApply`
    when `split_out > 1`

    This class is used within `ApplyConcatApply._lower`.

    See Also
    --------
    ApplyConcatApply
    """

    _parameters = [
        "frame",
        "kind",
        "_meta",
        "combine",
        "aggregate",
        "combine_kwargs",
        "aggregate_kwargs",
        "split_by",
        "split_every",
        "split_out",
        "sort",
        "shuffle_by_index",
    ]
    _defaults = {
        "split_every": 8,
        "split_out": True,
        "sort": None,
        "shuffle_by_index": None,
    }

    @property
    def split_out(self):
        if "split_out" in self._parameters:
            split_out = self.operand("split_out")
            if split_out is True:
                return self.frame.npartitions
            return split_out
        else:
            return 1

    def _lower(self):
        from dask_expr._repartition import Repartition
        from dask_expr._shuffle import SetIndexBlockwise, Shuffle, SortValues

        if is_index_like(self.frame._meta):
            columns = [self.frame._meta.name or "__index__"]
        elif is_series_like(self.frame._meta):
            columns = [self.frame._meta.name or "__series__"]
        else:
            columns = self.frame.columns

        # Find what columns we are shuffling by
        split_by = self.split_by or columns
        split_by_index = bool(set(split_by) - set(columns))

        # Make sure we have dataframe-like data to shuffle
        if split_by_index:
            if self.frame.ndim == 1:
                chunked = ResetIndex(self.frame, drop=False, name=self.frame.name)
            else:
                chunked = ResetIndex(self.frame, drop=False)
            if split_by == [None]:
                split_by = ["index"]
        elif is_index_like(self.frame._meta) or is_series_like(self.frame._meta):
            chunked = ToFrame(self.frame, name=columns[0])
        else:
            chunked = self.frame

        # Map Tuple[str] column names to str before the shuffle
        map_columns = {col: str(col) for col in chunked.columns if col != str(col)}
        unmap_columns = {v: k for k, v in map_columns.items()}
        if map_columns:
            chunked = RenameFrame(chunked, map_columns)

        # Sort or shuffle
        split_every = getattr(self, "split_every", 0) or chunked.npartitions
        ignore_index = getattr(self, "ignore_index", True)
        if self.shuffle_by_index is not None:
            ignore_index = not self.shuffle_by_index
        shuffle_npartitions = max(
            chunked.npartitions // split_every,
            self.split_out,
        )
        if self.sort:
            shuffled = SortValues(
                chunked,
                split_by,
                npartitions=shuffle_npartitions,
                ignore_index=ignore_index,
            )
        else:
            shuffled = Shuffle(
                chunked,
                split_by,
                shuffle_npartitions,
                ignore_index=ignore_index,
                index_shuffle=not split_by_index and self.shuffle_by_index,
            )

        # Unmap column names if necessary
        if unmap_columns:
            shuffled = RenameFrame(shuffled, unmap_columns)

        # Reset the index if we we used it for shuffling
        if split_by_index:
            divisions = (None,) * (shuffle_npartitions + 1)
            shuffled = SetIndexBlockwise(shuffled, split_by, True, divisions)

        # Convert back to Series if necessary
        if is_series_like(self._meta):
            shuffled = shuffled[shuffled.columns[0]]
        elif is_index_like(self._meta):
            column = shuffled.columns[0]
            shuffled = Index(
                SetIndexBlockwise(shuffled, column, True, shuffled.divisions)
            )
            if column == "__index__":
                shuffled = RenameSeries(shuffled, self.frame._meta.name)

        # Blockwise aggregate
        result = Aggregate(
            shuffled,
            self.kind,
            self.aggregate,
            self.aggregate_kwargs,
        )

        # Repartition and return
        if self.split_out < result.npartitions:
            return Repartition(result, new_partitions=self.split_out)
        return result

    @property
    def _meta(self):
        return self.operand("_meta")

    def _divisions(self):
        return (None,) * (self.split_out + 1)

    def __str__(self):
        chunked = str(self.frame)
        split_every = getattr(self, "split_every", 0)
        return f"{type(self).__name__}({chunked}, kind={funcname(self.kind)}, split_every={split_every})"


class TreeReduce(Expr):
    """Tree-reduction component of `ApplyConcatApply`

    This class is used within `ApplyConcatApply._lower`.

    See Also
    --------
    ApplyConcatApply
    """

    _parameters = [
        "frame",
        "kind",
        "_meta",
        "combine",
        "aggregate",
        "combine_kwargs",
        "aggregate_kwargs",
        "split_every",
    ]
    _defaults = {"split_every": 8}

    @functools.cached_property
    def _name(self):
        if funcname(self.combine) in ("combine", "aggregate"):
            name = funcname(self.combine.__self__).lower() + "-tree"
        else:
            name = funcname(self.combine)
        return name + "-" + _tokenize_deterministic(*self.operands)

    def __dask_postcompute__(self):
        return toolz.first, ()

    @functools.cached_property
    def split_every(self):
        if self.operand("split_every") is None:
            return 8
        return self.operand("split_every")

    def _layer(self):
        # apply combine to batches of intermediate results
        j = 1
        d = {}
        keys = self.frame.__dask_keys__()
        split_every = self.split_every
        while len(keys) > 1:
            new_keys = []
            for i, batch in enumerate(
                toolz.partition_all(split_every or len(keys), keys)
            ):
                batch = list(batch)
                if self.combine_kwargs:
                    d[self._name, j, i] = (
                        apply,
                        self.combine,
                        [batch],
                        self.combine_kwargs,
                    )
                else:
                    d[self._name, j, i] = (self.combine, batch)
                new_keys.append((self._name, j, i))
            j += 1
            keys = new_keys

        # apply aggregate to the final result
        d[self._name, 0] = (apply, self.aggregate, [keys], self.aggregate_kwargs)

        return d

    @property
    def _meta(self):
        return self.operand("_meta")

    def _divisions(self):
        return (None, None)

    def __str__(self):
        chunked = str(self.frame)
        split_every = getattr(self, "split_every", 0)
        return f"{type(self).__name__}({chunked}, kind={funcname(self.kind)}, split_every={split_every})"

    def _tree_repr_lines(self, indent=0, recursive=True):
        header = f"{funcname(self.kind)}({funcname(type(self))}):"
        lines = []
        if recursive:
            for dep in self.dependencies():
                lines.extend(dep._tree_repr_lines(2))

        split_every = getattr(self, "split_every", 0)
        header += f" split_every={split_every}"

        lines = [header] + lines
        lines = [" " * indent + line for line in lines]
        return lines


class ApplyConcatApply(Expr):
    """Perform reduction-like operation on dataframes

    This pattern is commonly used for reductions, groupby-aggregations, and
    more.  It requires three methods to be implemented:

    -   `chunk`: applied to each input partition
    -   `combine`: applied to lists of intermediate partitions as they are
        combined in batches
    -   `aggregate`: applied at the end to finalize the computation

    These methods should be easy to serialize, and can take in keyword
    arguments defined in `chunks/combine/aggregate_kwargs`.

    In many cases people don't define all three functions.  In these cases
    combine takes from aggregate and aggregate takes from chunk.
    """

    _parameters = ["frame"]
    chunk = None
    combine = None
    aggregate = None
    chunk_kwargs = {}
    combine_kwargs = {}
    aggregate_kwargs = {}
    _chunk_cls = Chunk

    @property
    def split_out(self):
        if "split_out" in self._parameters:
            split_out = self.operand("split_out")
            if isinstance(split_out, Callable):
                split_out = split_out(self.frame.npartitions)
            return split_out
        else:
            return 1

    def _layer(self):
        # This is an abstract expression
        raise NotImplementedError()

    @functools.cached_property
    def _meta_chunk(self):
        meta = meta_nonempty(self.frame._meta)
        return self.chunk(meta, **self.chunk_kwargs)

    @functools.cached_property
    def _meta(self):
        meta = self._meta_chunk
        aggregate = self.aggregate or (lambda x: x)
        if self.combine:
            combine = self.combine
            combine_kwargs = self.combine_kwargs
        else:
            combine = aggregate
            combine_kwargs = self.aggregate_kwargs

        meta = combine([meta], **combine_kwargs)
        meta = aggregate([meta], **self.aggregate_kwargs)
        return make_meta(meta)

    def _divisions(self):
        if getattr(self, "sort", False):
            return (None, None)
        if self.split_out is True:
            return (None,) * (self.frame.npartitions + 1)
        return (None,) * (self.split_out + 1)

    @property
    def _chunk_cls_args(self):
        return []

    def _lower(self):
        # Normalize functions in case not all are defined
        chunk = self.chunk
        chunk_kwargs = self.chunk_kwargs
        if self.aggregate:
            aggregate = self.aggregate
            aggregate_kwargs = self.aggregate_kwargs
        else:
            aggregate = chunk
            aggregate_kwargs = chunk_kwargs

        if self.combine:
            combine = self.combine
            combine_kwargs = self.combine_kwargs
        else:
            combine = aggregate
            combine_kwargs = aggregate_kwargs

        sort = getattr(self, "sort", False)
        split_every = getattr(self, "split_every", None)
        chunked = self._chunk_cls(
            self.frame, type(self), chunk, chunk_kwargs, *self._chunk_cls_args
        )
        if not isinstance(self.split_out, bool) and self.split_out == 1 or sort:
            # Lower into TreeReduce(Chunk)
            return TreeReduce(
                chunked,
                type(self),
                self._meta,
                combine,
                aggregate,
                combine_kwargs,
                aggregate_kwargs,
                split_every=split_every,
            )

        # Lower into ShuffleReduce
        return ShuffleReduce(
            chunked,
            type(self),
            self._meta,
            combine,
            aggregate,
            combine_kwargs,
            aggregate_kwargs,
            split_by=self.split_by,
            split_out=self.split_out,
            split_every=split_every,
            sort=sort,
            shuffle_by_index=getattr(self, "shuffle_by_index", None),
        )


class Unique(ApplyConcatApply):
    _parameters = ["frame", "split_every", "split_out"]
    _defaults = {"split_every": None, "split_out": True}
    chunk = staticmethod(methods.unique)
    aggregate_func = staticmethod(methods.unique)

    @functools.cached_property
    def _meta(self):
        return self.chunk(
            meta_nonempty(self.frame._meta), series_name=self.frame._meta.name
        )

    @property
    def split_by(self):
        return self.name

    @property
    def chunk_kwargs(self):
        return {"series_name": self._meta.name}

    @property
    def aggregate_kwargs(self):
        return self.chunk_kwargs

    @classmethod
    def combine(cls, inputs: list, **kwargs):
        return _concat(inputs)

    @classmethod
    def aggregate(cls, inputs: list, **kwargs):
        df = _concat(inputs)
        return cls.aggregate_func(df, **kwargs)


class DropDuplicates(Unique):
    _parameters = [
        "frame",
        "subset",
        "ignore_index",
        "split_every",
        "split_out",
        "keep",
    ]
    _defaults = {
        "subset": None,
        "ignore_index": False,
        "split_every": None,
        "split_out": 1,
        "keep": "first",
    }
    chunk = M.drop_duplicates
    aggregate_func = M.drop_duplicates

    @property
    def split_by(self):
        return self.subset

    @functools.cached_property
    def _meta(self):
        return self.chunk(meta_nonempty(self.frame._meta), **self.chunk_kwargs)

    @property
    def chunk_kwargs(self):
        out = {"keep": self.keep}
        if is_dataframe_like(self.frame._meta):
            out["subset"] = self.subset
        if PANDAS_GE_200 and not is_index_like(self.frame._meta):
            out["ignore_index"] = self.ignore_index
        return out

    def _simplify_up(self, parent, dependents):
        if self.subset is not None and isinstance(parent, Projection):
            columns = determine_column_projection(
                self, parent, dependents, additional_columns=self.subset
            )
            if columns == set(self.frame.columns):
                # Don't add unnecessary Projections, protects against loops
                return

            columns = [col for col in self.frame.columns if col in columns]
            return type(parent)(
                type(self)(self.frame[columns], *self.operands[1:]),
                *parent.operands[1:],
            )


class PivotTable(ApplyConcatApply):
    _parameters = ["frame", "columns", "index", "values", "aggfunc"]
    _defaults = {"columns": None, "index": None, "values": None, "aggfunc": "mean"}

    @functools.cached_property
    def _meta(self):
        df = self.frame._meta
        columns = self.operand("columns")
        values = self.operand("values")
        index = self.operand("index")
        columns_contents = pd.CategoricalIndex(df[columns].cat.categories, name=columns)

        if is_scalar(values):
            new_columns = columns_contents
        else:
            new_columns = pd.MultiIndex.from_product(
                (sorted(values), columns_contents), names=[None, columns]
            )

        if self.operand("aggfunc") in ["first", "last"]:
            # Infer datatype as non-numeric values are allowed
            if is_scalar(values):
                meta = pd.DataFrame(
                    columns=new_columns,
                    dtype=df[values].dtype,
                    index=pd.Index(df[index]),
                )
            else:
                meta = pd.DataFrame(
                    columns=new_columns,
                    index=pd.Index(df[index]),
                )
                for value_col in values:
                    meta[value_col] = meta[value_col].astype(
                        df[values].dtypes[value_col]
                    )
        else:
            # Use float64 as other aggregate functions require numerical data
            meta = pd.DataFrame(
                columns=new_columns, dtype=np.float64, index=pd.Index(df[index])
            )
        return meta

    def _lower(self):
        args = [
            self.frame,
            self.operand("columns"),
            self.operand("index"),
            self.operand("values"),
        ]
        if self.aggfunc == "sum":
            return PivotTableSum(*args)
        elif self.aggfunc == "mean":
            return PivotTableSum(*args) / PivotTableCount(*args)
        elif self.aggfunc == "count":
            return PivotTableCount(*args)
        elif self.aggfunc == "first":
            return PivotTableFirst(*args)
        elif self.aggfunc == "last":
            return PivotTableLast(*args)
        else:
            raise NotImplementedError(f"{self.aggfunc=} is not implemented")


class PivotTableAbstract(ApplyConcatApply):
    _parameters = ["frame", "columns", "index", "values", "aggfunc"]
    _defaults = {"columns": None, "index": None, "values": None, "aggfunc": "mean"}

    @property
    def chunk_kwargs(self):
        return {
            "index": self.operand("index"),
            "columns": self.operand("columns"),
            "values": self.operand("values"),
        }

    @classmethod
    def combine(cls, inputs: list, **kwargs):
        return _concat(inputs)

    @classmethod
    def aggregate(cls, inputs: list, **kwargs):
        df = _concat(inputs)
        return cls.aggregate_func(df, **kwargs)


class PivotTableSum(PivotTableAbstract):
    chunk = staticmethod(methods.pivot_sum)
    aggregate_func = staticmethod(methods.pivot_agg)


class PivotTableCount(PivotTableAbstract):
    chunk = staticmethod(methods.pivot_count)
    aggregate_func = staticmethod(methods.pivot_agg)


class PivotTableFirst(PivotTableAbstract):
    chunk = staticmethod(methods.pivot_first)
    aggregate_func = staticmethod(methods.pivot_agg_first)


class PivotTableLast(PivotTableAbstract):
    chunk = staticmethod(methods.pivot_last)
    aggregate_func = staticmethod(methods.pivot_agg_last)


class Reduction(ApplyConcatApply):
    """A common pattern of apply concat apply

    Common reductions like sum/min/max/count/... have some shared code around
    `_concat` and so on.  This class inherits from `ApplyConcatApply` in order
    to leverage this shared structure.

    I wouldn't be surprised if there was a way to merge them both into a single
    abstraction in the future.

    This class implements `{chunk,combine,aggregate}` methods of
    `ApplyConcatApply` by depending on `reduction_{chunk,combine,aggregate}`
    methods.
    """

    _defaults = {
        "skipna": True,
        "numeric_only": False,
        "min_count": 0,
        "dropna": True,
    }
    reduction_chunk = None
    reduction_combine = None
    reduction_aggregate = None

    @property
    def _projection_columns(self):
        return self.frame.columns

    @classmethod
    def chunk(cls, df, **kwargs):
        out = cls.reduction_chunk(df, **kwargs)
        # Return a dataframe so that the concatenated version is also a dataframe
        return out.to_frame().T if is_series_like(out) else out

    @classmethod
    def combine(cls, inputs: list, **kwargs):
        func = cls.reduction_combine or cls.reduction_aggregate or cls.reduction_chunk
        df = _concat(inputs)
        out = func(df, **kwargs)
        # Return a dataframe so that the concatenated version is also a dataframe
        return out.to_frame().T if is_series_like(out) else out

    @classmethod
    def aggregate(cls, inputs, **kwargs):
        func = cls.reduction_aggregate or cls.reduction_chunk
        df = _concat(inputs)
        return func(df, **kwargs)

    def __dask_postcompute__(self):
        return toolz.first, ()

    def _divisions(self):
        if self.ndim == 0 or len(self.frame.columns) == 0:
            return (None, None)
        return (min(self.frame.columns), max(self.frame.columns))

    def __str__(self):
        params = {param: self.operand(param) for param in self._parameters[1:]}
        s = ", ".join(
            k + "=" + repr(v) for k, v in params.items() if v != self._defaults.get(k)
        )
        base = str(self.frame)
        if " " in base:
            base = "(" + base + ")"
        return f"{base}.{self.__class__.__name__.lower()}({s})"

    def _simplify_up(self, parent, dependents):
        if isinstance(parent, Projection):
            return plain_column_projection(self, parent, dependents)


class Sum(Reduction):
    _parameters = ["frame", "skipna", "numeric_only", "min_count", "split_every"]
    _defaults = {
        "split_every": False,
        "numeric_only": False,
        "min_count": 0,
        "skipna": True,
    }
    reduction_chunk = M.sum

    @property
    def chunk_kwargs(self):
        return dict(
            skipna=self.skipna,
            numeric_only=self.numeric_only,
            min_count=self.min_count,
        )


class Prod(Reduction):
    _parameters = ["frame", "skipna", "numeric_only", "min_count", "split_every"]
    _defaults = {
        "split_every": False,
        "numeric_only": False,
        "min_count": 0,
        "skipna": True,
    }
    reduction_chunk = M.prod

    @property
    def chunk_kwargs(self):
        return dict(
            skipna=self.skipna,
            numeric_only=self.numeric_only,
            min_count=self.min_count,
        )


class Max(Reduction):
    _parameters = ["frame", "skipna", "numeric_only", "min_count", "split_every"]
    _defaults = {
        "split_every": False,
        "numeric_only": False,
        "min_count": 0,
        "skipna": True,
    }
    reduction_chunk = M.max

    @property
    def chunk_kwargs(self):
        return dict(
            skipna=self.skipna,
        )


class Min(Max):
    reduction_chunk = M.min


class Any(Reduction):
    _parameters = ["frame", "skipna", "split_every"]
    _defaults = {"skipna": True, "split_every": False}
    reduction_chunk = M.any

    @property
    def chunk_kwargs(self):
        return dict(
            skipna=self.skipna,
        )


class All(Reduction):
    _parameters = ["frame", "skipna", "split_every"]
    _defaults = {"split_every": False}
    reduction_chunk = M.all

    @property
    def chunk_kwargs(self):
        return dict(
            skipna=self.skipna,
        )


class IdxMin(Reduction):
    _parameters = ["frame", "skipna", "numeric_only"]
    reduction_chunk = idxmaxmin_chunk
    reduction_combine = idxmaxmin_combine
    reduction_aggregate = idxmaxmin_agg
    _required_attribute = "idxmin"

    @property
    def chunk_kwargs(self):
        # TODO: Add numeric_only after Dask release on May 26th
        return dict(skipna=self.skipna, fn=self._required_attribute)

    @property
    def combine_kwargs(self):
        return dict(skipna=self.skipna, fn=self._required_attribute)

    @property
    def aggregate_kwargs(self):
        return {**self.chunk_kwargs, "scalar": is_series_like(self.frame._meta)}


class IdxMax(IdxMin):
    _required_attribute = "idxmax"


class Len(Reduction):
    reduction_chunk = staticmethod(len)
    reduction_aggregate = sum

    def _simplify_down(self):
        from dask_expr.io.io import IO

        # We introduce Index nodes sometimes.  We special case around them.
        if isinstance(self.frame, Index) and self.frame.frame._is_length_preserving:
            return Len(self.frame.frame)

        # Pass through Elemwises, unless we just introduced an Index
        if self.frame._is_length_preserving and not isinstance(self.frame, Index):
            child = max(self.frame.dependencies(), key=lambda expr: expr.npartitions)
            return Len(child)

        # Let the child handle it.  They often know best
        if isinstance(self.frame, IO):
            return self

        if isinstance(self.frame, Concat) and self.frame.operand("axis") == 0:
            return sum(Len(obj) for obj in self.frame.dependencies())

        # Drop all of the columns, just pass through the index
        if self.frame.ndim == 2 and len(self.frame.columns):
            return Len(self.frame.index)

    def _simplify_up(self, parent, dependents):
        return


class Size(Reduction):
    reduction_aggregate = sum

    @staticmethod
    def reduction_chunk(df):
        return df.size

    def _simplify_down(self):
        if is_dataframe_like(self.frame._meta) and len(self.frame.columns) > 1:
            return len(self.frame.columns) * Len(self.frame)
        else:
            return Len(self.frame)

    def _simplify_up(self, parent, dependents):
        return


class NBytes(Reduction):
    # Only supported for Series objects
    reduction_aggregate = sum
    _required_attribute = "nbytes"

    @staticmethod
    def reduction_chunk(ser):
        return ser.nbytes


class Var(Reduction):
    # Uses the parallel version of Welford's online algorithm (Chan 79')
    # (http://i.stanford.edu/pub/cstr/reports/cs/tr/79/773/CS-TR-79-773.pdf)
    _parameters = ["frame", "skipna", "ddof", "numeric_only", "split_every"]
    _defaults = {"skipna": True, "ddof": 1, "numeric_only": False, "split_every": False}

    @functools.cached_property
    def _meta(self):
        return make_meta(
            meta_nonempty(self.frame._meta).var(
                skipna=self.skipna, numeric_only=self.numeric_only
            )
        )

    @property
    def chunk_kwargs(self):
        return dict(skipna=self.skipna, numeric_only=self.numeric_only)

    @property
    def combine_kwargs(self):
        return {}

    @property
    def aggregate_kwargs(self):
        return dict(ddof=self.ddof)

    @classmethod
    def reduction_chunk(cls, x, skipna=True, numeric_only=False):
        kwargs = {"numeric_only": numeric_only} if is_dataframe_like(x) else {}
        if skipna or numeric_only:
            n = x.count(**kwargs)
            kwargs["skipna"] = skipna
            avg = x.mean(**kwargs)
        else:
            # Not skipping nulls, so might as well
            # avoid the full `count` operation
            n = len(x)
            kwargs["skipna"] = skipna
            avg = x.sum(**kwargs) / n
        if numeric_only:
            # Workaround for cudf bug
            # (see: https://github.com/rapidsai/cudf/issues/13731)
            x = x.select_dtypes("number")
            n = n.loc[x.columns]
        m2 = ((x - avg) ** 2).sum(**kwargs)
        return n, avg, m2

    @classmethod
    def reduction_combine(cls, parts):
        n, avg, m2 = parts[0]
        for i in range(1, len(parts)):
            n_a, avg_a, m2_a = n, avg, m2
            n_b, avg_b, m2_b = parts[i]
            n = n_a + n_b
            avg = (n_a * avg_a + n_b * avg_b) / n
            delta = avg_b - avg_a
            m2 = m2_a + m2_b + delta**2 * n_a * n_b / n
        return n, avg, m2

    @classmethod
    def reduction_aggregate(cls, vals, ddof=1):
        vals = cls.reduction_combine(vals)
        n, _, m2 = vals
        return m2 / (n - ddof)


class Mean(Reduction):
    _parameters = ["frame", "skipna", "numeric_only", "split_every"]
    _defaults = {"skipna": True, "numeric_only": False, "split_every": False}

    @functools.cached_property
    def _meta(self):
        return (
            self.frame._meta.sum(skipna=self.skipna, numeric_only=self.numeric_only) / 2
        )

    def _lower(self):
        return self.frame.sum(
            skipna=self.skipna,
            numeric_only=self.numeric_only,
            split_every=self.split_every,
        ) / self.frame.count(split_every=self.split_every)


class Count(Reduction):
    _parameters = ["frame", "numeric_only", "split_every"]
    _defaults = {"split_every": False, "numeric_only": False}
    reduction_chunk = M.count

    @classmethod
    def reduction_aggregate(cls, df):
        return df.sum().astype("int64")


class Mode(ApplyConcatApply):
    """

    Mode was a bit more complicated than class reductions, so we retreat back
    to ApplyConcatApply
    """

    _parameters = ["frame", "dropna", "split_every"]
    _defaults = {"dropna": True, "split_every": False}
    chunk = M.value_counts

    @classmethod
    def combine(cls, results: list[pd.Series]):
        df = _concat(results)
        out = df.groupby(df.index).sum()
        out.name = results[0].name
        return out

    @classmethod
    def aggregate(cls, results: list[pd.Series], dropna=None):
        [df] = results
        max = df.max(skipna=dropna)
        out = df[df == max].index.to_series().sort_values().reset_index(drop=True)
        return out

    @property
    def chunk_kwargs(self):
        return {"dropna": self.dropna}

    @property
    def aggregate_kwargs(self):
        return {"dropna": self.dropna}


class NuniqueApprox(Reduction):
    _parameters = ["frame", "b", "split_every"]
    _defaults = {"b": 16, "split_every": None}
    reduction_chunk = hyperloglog.compute_hll_array
    reduction_combine = hyperloglog.reduce_state
    reduction_aggregate = hyperloglog.estimate_count

    @functools.cached_property
    def _meta(self):
        return 1.0

    @property
    def chunk_kwargs(self):
        return {"b": self.b}

    @property
    def combine_kwargs(self):
        return self.chunk_kwargs

    @property
    def aggregate_kwargs(self):
        return self.chunk_kwargs


class ReductionConstantDim(Reduction):
    """
    Some reductions reduce the number of rows in your object but keep the original
    dimension, e.g. a DataFrame stays a DataFrame instead of getting reduced to
    a Series.
    """

    @classmethod
    def chunk(cls, df, **kwargs):
        return cls.reduction_chunk(df, **kwargs)

    @classmethod
    def combine(cls, inputs: list, **kwargs):
        func = cls.reduction_combine or cls.reduction_aggregate or cls.reduction_chunk
        df = _concat(inputs)
        return func(df, **kwargs)

    def _divisions(self):
        # TODO: We can do better in some cases
        return (None, None)


class NLargest(ReductionConstantDim):
    _defaults = {"n": 5, "_columns": None}
    _parameters = ["frame", "n", "_columns"]
    reduction_chunk = M.nlargest
    reduction_aggregate = M.nlargest

    def _columns_kwarg(self):
        if self._columns is None:
            return {}
        return {"columns": self._columns}

    @property
    def chunk_kwargs(self):
        return {"n": self.n, **self._columns_kwarg()}

    @property
    def combine_kwargs(self):
        return self.chunk_kwargs

    @property
    def aggregate_kwargs(self):
        return self.chunk_kwargs


def _nsmallest_slow(df, columns, n):
    return df.sort_values(by=columns).head(n)


def _nlargest_slow(df, columns, n):
    return df.sort_values(by=columns).tail(n)


class NLargestSlow(NLargest):
    reduction_chunk = _nlargest_slow
    reduction_aggregate = _nlargest_slow


class NSmallest(NLargest):
    _parameters = ["frame", "n", "_columns"]
    reduction_chunk = M.nsmallest
    reduction_aggregate = M.nsmallest


class NSmallestSlow(NLargest):
    reduction_chunk = _nsmallest_slow
    reduction_aggregate = _nsmallest_slow


class ValueCounts(ReductionConstantDim):
    _defaults = {
        "sort": None,
        "ascending": False,
        "dropna": True,
        "normalize": False,
        "split_every": None,
        "split_out": 1,
    }

    _parameters = [
        "frame",
        "sort",
        "ascending",
        "dropna",
        "normalize",
        "split_every",
        "split_out",
    ]
    reduction_chunk = M.value_counts
    reduction_aggregate = methods.value_counts_aggregate
    reduction_combine = methods.value_counts_combine

    @property
    def chunk_kwargs(self):
        return {"sort": self.sort, "ascending": self.ascending, "dropna": self.dropna}

    @property
    def aggregate_kwargs(self):
        return {**self.chunk_kwargs, "normalize": self.normalize}

    @property
    def combine_kwargs(self):
        return self.chunk_kwargs

    def _simplify_up(self, parent, dependents):
        # We are already a Series
        return


class MemoryUsage(Reduction):
    reduction_chunk = M.memory_usage
    reduction_aggregate = M.sum
    split_every = 0

    def _divisions(self):
        # TODO: We can do better, but not high priority
        return (None, None)


class MemoryUsageIndex(MemoryUsage):
    _parameters = ["frame", "deep"]
    _defaults = {"deep": False}

    @property
    def chunk_kwargs(self):
        return {"deep": self.deep}


class MemoryUsageFrame(MemoryUsage):
    _parameters = ["frame", "deep", "_index"]
    _defaults = {"deep": False, "_index": True}

    @property
    def chunk_kwargs(self):
        return {"deep": self.deep, "index": self._index}

    @property
    def combine_kwargs(self):
        return {"is_dataframe": is_dataframe_like(self.frame._meta)}

    @staticmethod
    def reduction_combine(x, is_dataframe):
        if is_dataframe:
            return x.groupby(x.index).sum()
        return x.sum()


class TotalMemoryUsageFrame(MemoryUsageFrame):
    reduction_chunk = total_mem_usage

    @staticmethod
    def reduction_combine(x, is_dataframe):
        return x


class IsMonotonicIncreasing(Reduction):
    reduction_chunk = methods.monotonic_increasing_chunk
    reduction_combine = methods.monotonic_increasing_combine
    reduction_aggregate = methods.monotonic_increasing_aggregate


class IsMonotonicDecreasing(Reduction):
    reduction_chunk = methods.monotonic_decreasing_chunk
    reduction_combine = methods.monotonic_decreasing_combine
    reduction_aggregate = methods.monotonic_decreasing_aggregate
