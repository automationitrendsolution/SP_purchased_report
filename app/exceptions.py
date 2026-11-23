"""Exception hierarchy.

Every failure mode the pipeline can hit has its own type so that callers can
decide independently whether to retry, alert, or abort.  ``transient`` marks the
errors that are safe to retry; authentication/CAPTCHA/MFA failures are never
transient and must never be retried automatically.
"""

from __future__ import annotations


class ReportFlowError(Exception):
    """Base class for every pipeline failure."""

    #: Whether retrying the same operation could plausibly succeed.
    transient: bool = False
    #: Process exit code used when this error reaches the top level.
    exit_code: int = 1


class ConfigurationError(ReportFlowError):
    """Missing or invalid environment configuration."""

    exit_code = 2


class AuthenticationError(ReportFlowError):
    """Amazon session is missing, expired, or blocked by a security challenge.

    Never retried: repeated automated attempts against sign-in, CAPTCHA, OTP or
    MFA are exactly what Amazon's protections exist to stop.  A human must
    re-establish the session with ``python run.py --login``.
    """

    exit_code = 3


class NavigationError(ReportFlowError):
    """The Reports page did not reach the expected state."""

    transient = True
    exit_code = 4


class ReportBuildError(ReportFlowError):
    """A Create report control could not be found or set.

    Transient: most often a control that rendered late.  Only ever raised
    *before* Run report is clicked, so a retry cannot create a duplicate.
    """

    transient = True
    exit_code = 12


class ReportNotFoundError(ReportFlowError):
    """The report this run created never appeared in the Reports list.

    Not transient: either it was not created (the form did not submit) or the
    list markup changed, and looking again will not fix either.
    """

    exit_code = 5


class DownloadError(ReportFlowError):
    """The report is not ready, did not download, or produced no file."""

    transient = True
    exit_code = 6


class ReportValidationError(ReportFlowError):
    """Downloaded file is not a usable Purchased product report."""

    exit_code = 7


class DataTransformationError(ReportFlowError):
    """Report could not be mapped onto the target schema."""

    exit_code = 8


class DatabaseError(ReportFlowError):
    """MySQL connectivity or transaction failure.

    ``transient`` is set per instance by :mod:`app.database`: a dropped
    connection or a lost lock race is retried, a schema or data error is not.
    """

    exit_code = 9


class EmptyReportError(ReportValidationError):
    """Report parsed correctly but contained zero data rows.

    A hard stop so that an empty download can never delete good data.
    """

    exit_code = 10


class LockError(ReportFlowError):
    """Another run already holds the execution lock."""

    exit_code = 11
