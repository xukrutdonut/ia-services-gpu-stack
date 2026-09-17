import os
import time
import asyncio
from typing import List, Union, Optional
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
import uvicorn
from transformers import AutoTokenizer
from optimum.intel import OVModelForFeatureExtraction
import torch
import numpy as np

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
# Nota: Usamos all-MiniLM-L6-v2 porque Intel NPU no soporta nomic_bert nativamente
DEFAULT_MODEL_ID = os.getenv("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
DEVICE = os.getenv("OPENVINO_DEVICE", "NPU")
PORT = int(os.getenv("PORT", "8283"))  # Puerto distinto al del chat (8282)

app = FastAPI(title="OpenVINO NPU Embeddings Server")

# Caché global
_model = None
_tokenizer = None

def load_model():
    global _model, _tokenizer
    if _model is None:
        print(f"[*] Cargando {DEFAULT_MODEL_ID} en {DEVICE}...")
        _tokenizer = AutoTokenizer.from_pretrained(DEFAULT_MODEL_ID)
        # Export=True descarga los pesos de PyTorch y los convierte a OpenVINO IR
        _model = OVModelForFeatureExtraction.from_pretrained(DEFAULT_MODEL_ID, export=True)
        _model.to(DEVICE)
        print("[*] ¡Modelo cargado correctamente en la NPU!")

# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------
class EmbeddingRequest(BaseModel):
    input: Union[str, List[str]]
    model: Optional[str] = DEFAULT_MODEL_ID

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup_event():
    # Cargar el modelo en background al iniciar el servidor
    asyncio.create_task(asyncio.to_thread(load_model))

@app.post("/v1/embeddings")
async def create_embeddings(req: EmbeddingRequest):
    if _model is None:
        raise HTTPException(status_code=503, detail="Model is still loading. Please try again in a few seconds.")
    
    # Normalizar input a lista
    texts = req.input if isinstance(req.input, list) else [req.input]

    def do_infer():
        try:
            results = []
            with torch.no_grad():
                for text in texts:
                    # Tokenizar individualmente con padding/truncation estricto a 512
                    inputs = _tokenizer([text], padding="max_length", truncation=True, max_length=512, return_tensors="pt")
                    
                    # Asegurarnos de que el modelo esté reshapeado a (1, 512)
                    # Esto evita el error de "Got negative shape dim bound: '-1'" en la NPU
                    # La compilación se hace automáticamente en la primera inferencia
                    _model.reshape(1, 512)
                    
                    outputs = _model(**inputs)
                    
                    # Pooling (mean pooling)
                    attention_mask = inputs['attention_mask']
                    token_embeddings = outputs.last_hidden_state
                    
                    input_mask_expanded = attention_mask.unsqueeze(-1).expand(token_embeddings.size()).float()
                    sum_embeddings = torch.sum(token_embeddings * input_mask_expanded, 1)
                    sum_mask = torch.clamp(input_mask_expanded.sum(1), min=1e-9)
                    embedding = sum_embeddings / sum_mask
                    
                    # Normalizar los embeddings (L2)
                    embedding = torch.nn.functional.normalize(embedding, p=2, dim=1)
                    results.append(embedding.cpu().numpy()[0])
            return np.array(results)
        except Exception as e:
            import traceback
            traceback.print_exc()
            raise e

    embeddings_array = await asyncio.to_thread(do_infer)
    
    # Construir respuesta OpenAI Compatible
    data = []
    for i, emb in enumerate(embeddings_array):
        data.append({
            "object": "embedding",
            "index": i,
            "embedding": emb.tolist()
        })
        
    prompt_tokens = sum([len(t.split()) for t in texts])

    return {
        "object": "list",
        "data": data,
        "model": req.model,
        "usage": {
            "prompt_tokens": prompt_tokens,
            "total_tokens": prompt_tokens
        }
    }

if __name__ == "__main__":
    print(f"[*] Iniciando Servidor de Embeddings en puerto {PORT} usando la NPU...")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
