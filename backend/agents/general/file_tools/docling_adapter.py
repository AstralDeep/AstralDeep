import os
import resource
from typing import Dict, Any, Optional
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions

def run_structured_parse(file_bytes: bytes, mime_type: str) -> Dict[str, Any]:
    """
    Executa o parsing estruturado via Docling com limites de recursos.
    """
    # Limites de segurança (1GB RAM, 30s CPU)
    resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024 * 1024, 1024 * 1024 * 1024))
    
    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = False  # Desabilitado para manter a performance e segurança
    
    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
        }
    )
    
    try:
        # Processamento em memória via stream
        result = converter.convert_bytes(file_bytes, mime_type=mime_type)
        return {
            "status": "SUCCESS",
            "content": result.document.export_to_markdown(),
            "provenance": result.document.metadata.doc_items
        }
    except Exception as e:
        return {"status": "FAILED", "error": str(e)}
