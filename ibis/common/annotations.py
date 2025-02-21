from __future__ import annotations

import inspect
from typing import Any

from ibis.common.validators import option, tuple_of
from ibis.util import DotDict

EMPTY = inspect.Parameter.empty  # marker for missing argument
VAR_POSITIONAL = inspect.Parameter.VAR_POSITIONAL
POSITIONAL_OR_KEYWORD = inspect.Parameter.POSITIONAL_OR_KEYWORD
KEYWORD_ONLY = inspect.Parameter.KEYWORD_ONLY


def _noop(arg, **kwargs):
    return arg


class Annotation:
    """Base class for all annotations."""

    __slots__ = ('_validator',)

    def __init__(self, validator=None):
        self._validator = validator

    def validate(self, arg, **kwargs):
        if self._validator is None:
            return arg
        return self._validator(arg, **kwargs)


class Attribute(Annotation):
    """Annotation to mark a field in a class.

    An optional validator can be provider to validate the field every time it
    is set.

    Parameters
    ----------
    validator : Validator, default noop
        Validator to validate the field.
    """

    __slots__ = ('_default',)

    def __init__(self, validator=None, default=EMPTY):
        self._default = default
        self._validator = validator

    @classmethod
    def default(self, fn):
        return Attribute(default=fn)

    def __eq__(self, other):
        return (
            type(self) is type(other)
            and self._default == other._default
            and self._validator == other._validator
        )

    def initialize(self, this):
        if self._default is EMPTY:
            return None
        value = self._default(this)
        return self.validate(value, this=this)


class Argument(Annotation):
    """Base class for all fields which should be passed as arguments."""

    __slots__ = ('_kind', '_default')

    def __init__(self, kind, default=EMPTY, validator=None):
        self._kind = kind
        self._default = default
        self._validator = validator

    def __eq__(self, other):
        return (
            type(self) is type(other)
            and self._kind == other._kind
            and self._default == other._default
            and self._validator == other._validator
        )

    def replace(self, **kwargs):
        return self.__class__(
            kind=kwargs.get('kind', self._kind),
            default=kwargs.get('default', self._default),
            validator=kwargs.get('validator', self._validator),
        )

    @classmethod
    def mandatory(cls, validator=None):
        """Annotation to mark a mandatory argument."""
        return cls(POSITIONAL_OR_KEYWORD, validator=validator)

    @classmethod
    def mandatory_keyword(cls, validator=None):
        """Annotation to mark a mandatory keyword only argument."""
        return cls(KEYWORD_ONLY, validator=validator)

    @classmethod
    def default(cls, default, validator=None):
        """Annotation to allow missing arguments with a default value."""
        return cls(POSITIONAL_OR_KEYWORD, default=default, validator=validator)

    @classmethod
    def optional(cls, validator=None, default=None):
        """Annotation to allow and treat `None` values as missing arguments."""
        if validator is None:
            validator = option(_noop, default=default)
        else:
            validator = option(validator, default=default)
        return cls(POSITIONAL_OR_KEYWORD, default=None, validator=validator)

    @classmethod
    def variadic(cls, validator=None):
        """Annotation variadic positional arguments."""
        if validator is None:
            validator = tuple_of(_noop)
        else:
            validator = tuple_of(validator)
        return cls(VAR_POSITIONAL, validator=validator)


class Parameter(inspect.Parameter):
    """Augmented Parameter class to additionally hold a validator object."""

    __slots__ = ()

    def __init__(self, name, annotation):
        if not isinstance(annotation, Argument):
            raise TypeError(
                f'annotation must be an instance of Argument, got {annotation}'
            )
        super().__init__(
            name,
            kind=annotation._kind,
            default=annotation._default,
            annotation=annotation._validator,
        )

    def validate(self, arg, *, this):
        if self.annotation is None:
            return arg
        return self.annotation(arg, this=this)


class Signature(inspect.Signature):
    """Validatable signature.

    Primarly used in the implementation of
    ibis.common.grounds.Annotable.
    """

    __slots__ = ()

    @classmethod
    def merge(cls, *signatures, **annotations):
        """Merge multiple signatures.

        In addition to concatenating the parameters, it also reorders the
        parameters so that optional arguments come after mandatory arguments.

        Parameters
        ----------
        *signatures : Signature
            Signature instances to merge.
        **annotations : dict
            Annotations to add to the merged signature.

        Returns
        -------
        Signature
        """
        params = {}
        inherited = set()
        is_variadic = False
        for sig in signatures:
            for name, param in sig.parameters.items():
                is_variadic |= param.kind == VAR_POSITIONAL
                params[name] = param
                inherited.add(name)

        for name, annot in annotations.items():
            if is_variadic:
                annot = annot.replace(kind=KEYWORD_ONLY)
            param = Parameter(name, annotation=annot)
            is_variadic |= param.kind == VAR_POSITIONAL
            params[name] = param

        # mandatory fields without default values must preceed the optional
        # ones in the function signature, the partial ordering will be kept
        new_args, new_kwargs = [], []
        inherited_args, inherited_kwargs = [], []

        for name, param in params.items():
            if name in inherited:
                if param.default is EMPTY:
                    inherited_args.append(param)
                else:
                    inherited_kwargs.append(param)
            else:
                if param.default is EMPTY:
                    new_args.append(param)
                else:
                    new_kwargs.append(param)

        return cls(inherited_args + new_args + new_kwargs + inherited_kwargs)

    def unbind(self, this: Any):
        """Reverse bind of the parameters.

        Attempts to reconstructs the original arguments as positional and
        keyword arguments. Since keyword arguments are the preferred, the
        positional arguments are filled only if the signature has variadic
        args.

        Parameters
        ----------
        this : Any
            Object with attributes matching the signature parameters.

        Returns
        -------
        args : (args, kwargs)
            Tuple of positional and keyword arguments.
        """
        # does the reverse of bind, but doesn't apply defaults
        args, kwargs = (), {}

        for name, param in self.parameters.items():
            value = getattr(this, name)
            if param.kind == VAR_POSITIONAL:
                # need to adjust due to the variadic argument: fn(a, b, *args)
                # cannot be called again as fn(*args, a=..., b=...)
                args = tuple(kwargs.values()) + value
                kwargs = {}
            else:
                kwargs[name] = value

        return args, kwargs

    def validate(self, *args, **kwargs):
        """Validate the arguments against the signature.

        Parameters
        ----------
        args : tuple
            Positional arguments.
        kwargs : dict
            Keyword arguments.

        Returns
        -------
        validated : dict
            Dictionary of validated arguments.
        """
        # bind the signature to the passed arguments and apply the validators
        # before passing the arguments, so self.__init__() receives already
        # validated arguments as keywords
        bound = self.bind(*args, **kwargs)
        bound.apply_defaults()

        this = DotDict()
        for name, value in bound.arguments.items():
            param = self.parameters[name]
            # TODO(kszucs): provide more error context on failure
            this[name] = param.validate(value, this=this)

        return this


# aliases for convenience
attribute = Attribute
mandatory = Argument.mandatory
optional = Argument.optional
variadic = Argument.variadic
default = Argument.default
immutable_property = Attribute.default


# TODO(kszucs): try to cache validator objects
# TODO(kszucs): try a quicker curry implementation
