"""Inspector V2 em modo sombra, isolado de toda integração produtiva."""

from app.inspector_v2.engine import InspectorV2Engine
from app.inspector_v2.models import AnalysisPayload, AnalysisResult

__all__ = ["AnalysisPayload", "AnalysisResult", "InspectorV2Engine"]
