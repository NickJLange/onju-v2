"""
Whisper ASR server using faster-whisper (CTranslate2, INT8 + AVX-512).

Same /transcribe contract as pipeline/services/asr_server.py:
  POST /transcribe  multipart 'audio' = WAV (16kHz mono int16)
                    -> {"text": str, "duration_s": float, "transcribe_time_s": float}
  GET  /health      -> {"status": "ok"|"loading", "model": str}

Does NOT emit no_speech_prob — omitting it keeps this engine symmetric with
parakeet (emitting it activates a dormant silence gate in main.py:204).

Env vars:
    WHISPER_MODEL         faster-whisper model size (default: base.en)
    WHISPER_MODEL_DIR     model cache dir (default: /models)
    WHISPER_COMPUTE_TYPE  int8 | int8_float16 | float16 (default: int8)
"""

import argparse
import logging
import os
import tempfile
import time
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import JSONResponse

MODEL_NAME = os.environ.get("WHISPER_MODEL", "base.en")
MODEL_DIR = os.environ.get("WHISPER_MODEL_DIR", "/models")
COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("whisper")

_model = None

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB — guard against accidental/malicious oversized uploads


async def _load_model():
    global _model
    from faster_whisper import WhisperModel

    os.makedirs(MODEL_DIR, exist_ok=True)
    logger.info("Loading faster-whisper model '%s' compute_type=%s ...", MODEL_NAME, COMPUTE_TYPE)
    tic = time.time()
    try:
        _model = WhisperModel(MODEL_NAME, device="cpu", compute_type=COMPUTE_TYPE,
                              download_root=MODEL_DIR)
        logger.info("Model loaded in %.1fs", time.time() - tic)
    except Exception:
        logger.error("Failed to load model '%s'\n%s", MODEL_NAME, traceback.format_exc())
        raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _load_model()
    yield


app = FastAPI(lifespan=lifespan)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    logger.error("Unhandled exception on %s %s\n%s",
                 request.method, request.url.path, traceback.format_exc())
    return JSONResponse(status_code=500, content={"error": "internal server error"})


@app.get("/health")
async def health():
    return {"status": "ok" if _model else "loading", "model": MODEL_NAME}


@app.post("/transcribe")
async def transcribe(audio: UploadFile = File(...)):
    if _model is None:
        return JSONResponse(status_code=503, content={"error": "model not loaded"})

    raw = await audio.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        return JSONResponse(status_code=413, content={"error": "audio file too large"})
    ext = os.path.splitext(audio.filename or "audio.wav")[1] or ".wav"

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
        f.write(raw)
        tmp_path = f.name

    try:
        tic = time.time()
        segments, info = _model.transcribe(tmp_path, beam_size=5, language="en")
        # faster-whisper returns a generator — consume it to get all segments
        segments = list(segments)
        elapsed = time.time() - tic
    except Exception:
        logger.error("Transcription failed for %s\n%s", audio.filename, traceback.format_exc())
        raise
    finally:
        os.unlink(tmp_path)

    text = " ".join(s.text.strip() for s in segments).strip()
    duration_s = segments[-1].end if segments else info.duration

    return {
        "text": text,
        "duration_s": round(duration_s, 2),
        "transcribe_time_s": round(elapsed, 3),
    }


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="Whisper ASR server (faster-whisper)")
    parser.add_argument("--port", type=int, default=8101)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--model", default=None)
    parser.add_argument("--model-dir", default=None)
    parser.add_argument("--compute-type", default=None)
    args = parser.parse_args()

    if args.model:
        MODEL_NAME = args.model
    if args.model_dir:
        MODEL_DIR = args.model_dir
    if args.compute_type:
        COMPUTE_TYPE = args.compute_type

    uvicorn.run(app, host=args.host, port=args.port)
