"""
Whisper ASR server (whisper.cpp via pywhispercpp, Vulkan-accelerated).

Implements the same /transcribe contract as pipeline/services/asr_server.py:
  POST /transcribe  multipart field 'audio' = WAV (16kHz mono int16)
                    -> {"text": str, "duration_s": float, "transcribe_time_s": float}
  GET  /health      -> {"status": "ok"|"loading", "model": str}

Does NOT emit no_speech_prob — that field activates a dormant silence gate in
the gateway (main.py) and would give this engine asymmetric behaviour vs parakeet.

Run:
    python whisper_server.py
    python whisper_server.py --port 8101 --host 0.0.0.0 --model base.en

Env vars:
    WHISPER_MODEL      ggml model name (default: base.en)
    WHISPER_MODEL_DIR  model cache directory (default: /models)
"""

import logging
import os
import tempfile
import time
import traceback
import argparse

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import JSONResponse

MODEL_NAME = os.environ.get("WHISPER_MODEL", "base.en")
MODEL_DIR = os.environ.get("WHISPER_MODEL_DIR", "/models")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("whisper")

app = FastAPI()
_model = None


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    logger.error("Unhandled exception on %s %s\n%s",
                 request.method, request.url.path, traceback.format_exc())
    return JSONResponse(status_code=500, content={"error": str(exc)})


@app.on_event("startup")
async def load_model():
    global _model
    from pywhispercpp.model import Model

    os.makedirs(MODEL_DIR, exist_ok=True)
    logger.info("Loading whisper model '%s' from %s ...", MODEL_NAME, MODEL_DIR)
    tic = time.time()
    try:
        # n_threads=0 lets whisper.cpp pick optimal; Vulkan offload logged by the lib
        _model = Model(MODEL_NAME, models_dir=MODEL_DIR, n_threads=0)
        logger.info("Model loaded in %.1fs", time.time() - tic)
    except Exception:
        logger.error("Failed to load model '%s'\n%s", MODEL_NAME, traceback.format_exc())
        raise


@app.get("/health")
async def health():
    return {"status": "ok" if _model else "loading", "model": MODEL_NAME}


@app.post("/transcribe")
async def transcribe(audio: UploadFile = File(...)):
    raw = await audio.read()
    ext = os.path.splitext(audio.filename or "audio.wav")[1] or ".wav"

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
        f.write(raw)
        tmp_path = f.name

    try:
        tic = time.time()
        segments = _model.transcribe(tmp_path)
        elapsed = time.time() - tic
    except Exception:
        logger.error("Transcription failed for %s\n%s", audio.filename, traceback.format_exc())
        raise
    finally:
        os.unlink(tmp_path)

    text = " ".join(s.text.strip() for s in segments).strip()
    # t1 is in 10ms units; last segment end = audio duration
    duration_s = (segments[-1].t1 * 0.01) if segments else 0.0

    return {
        "text": text,
        "duration_s": round(duration_s, 2),
        "transcribe_time_s": round(elapsed, 3),
    }


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="Whisper ASR server (Vulkan)")
    parser.add_argument("--port", type=int, default=8101)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--model", default=None)
    parser.add_argument("--model-dir", default=None)
    args = parser.parse_args()

    if args.model:
        MODEL_NAME = args.model
    if args.model_dir:
        MODEL_DIR = args.model_dir

    uvicorn.run(app, host=args.host, port=args.port)
