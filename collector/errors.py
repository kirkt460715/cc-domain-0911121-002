"""门户采集的结构化错误。

每个错误都带有可读的中文解释、所处阶段与可检查的上下文，
保证登录失效或筛选项撤回时流程停在可理解的位置，而不是吞掉异常。
"""


class CollectorError(Exception):
    stage = 'collector'
    kind = 'collector_error'

    def __init__(self, message, context=None):
        super().__init__(message)
        self.message = message
        self.context = context or {}

    def document(self):
        return {'error': self.kind, 'stage': self.stage, 'message': self.message,
                'context': self.context}


class PortalUnavailable(CollectorError):
    stage = 'portal'
    kind = 'portal_unavailable'


class LoginExpired(CollectorError):
    stage = 'session'
    kind = 'login_expired'


class FilterWithdrawn(CollectorError):
    stage = 'filters'
    kind = 'filter_withdrawn'


class OptionsStale(CollectorError):
    stage = 'filters'
    kind = 'options_stale'


class ExportTimeout(CollectorError):
    stage = 'export'
    kind = 'export_timeout'


class DownloadMismatch(CollectorError):
    stage = 'download'
    kind = 'download_mismatch'


class ProvenanceMismatch(CollectorError):
    stage = 'provenance'
    kind = 'provenance_mismatch'


class IngestionRejected(CollectorError):
    stage = 'ingestion'
    kind = 'ingestion_rejected'
