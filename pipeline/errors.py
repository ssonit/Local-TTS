"""Errors the job server can show in the UI."""


class PipelineError(RuntimeError):
    pass


class NoCaptionsError(PipelineError):
    """English captions were not available, so speech recognition should run."""
