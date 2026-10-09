from .docling_adapter import run_structured_parse

def read_document(file_bytes: bytes, mime_type: str, use_structured_parser: bool = False) -> str:
    if use_structured_parser:
        result = run_structured_parse(file_bytes, mime_type)
        if result["status"] == "SUCCESS":
            return result["content"]
    
    # Fallback para o leitor legado
    return _legacy_text_extraction(file_bytes)

def _legacy_text_extraction(file_bytes: bytes) -> str:
    # Implementação existente mantida para compatibilidade
    return "..."
