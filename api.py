import uuid
import hashlib
import shutil
import os
from pathlib import Path
import logging
from datetime import datetime, timedelta
from typing import Optional, List

import multiprocessing # Added import

from fastapi import FastAPI, HTTPException, UploadFile, File, BackgroundTasks, Depends, status as fastapi_status
from fastapi.responses import FileResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel

# --- Attempt to import security libraries, with mocks as fallback ---
try:
    from jose import JWTError, jwt
    jwt_available = True
except ImportError:
    jwt_available = False
    logger = logging.getLogger(__name__)
    logger.warning("python-jose not found. Using mock JWT implementation. THIS IS NOT SECURE.")
    class JWTError(Exception): pass
    class jwt:
        @staticmethod
        def encode(claims, key, algorithm):
            return f"fake-jwt-{claims.get('sub')}"
        @staticmethod
        def decode(token, key, algorithms):
            if not token.startswith("fake-jwt-"):
                raise JWTError("Invalid fake token")
            return {"sub": token.replace("fake-jwt-", "")}

try:
    from passlib.context import CryptContext
    passlib_available = True
except ImportError:
    passlib_available = False
    logger = logging.getLogger(__name__)
    logger.warning("passlib not found. Using mock password hashing. THIS IS NOT SECURE.")
    class CryptContext:
        def __init__(self, schemes=None, deprecated=None): pass
        def verify(self, secret, hash): return secret == hash
        def hash(self, secret): return secret

# Actual imports from the project
from lib.functions import (
    SessionContext, # Ensured import
    convert_ebook,
    get_sanitized,
    prepare_dirs
)
from lib.conf import (
    default_device,
    default_tts_engine,
    default_output_format,
    audiobooks_gui_dir,
    tmp_dir,
    prog_version,
    ebook_formats,
    min_python_version,
    max_python_version,
    default_xtts_settings,
    default_bark_settings,
    models as tts_models
)
from lib.lang import default_language_code, language_mapping

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Authentication Settings & Utilities ---
SECRET_KEY = "your-secret-key-here"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")

fake_users_db = {
    "testuser": {
        "username": "testuser",
        "email": "test@example.com",
        "hashed_password": pwd_context.hash("testpassword"),
        "disabled": False,
    }
}

# --- Pydantic Models for Auth ---
class Token(BaseModel):
    access_token: str
    token_type: str

class TokenData(BaseModel):
    username: Optional[str] = None

class User(BaseModel):
    username: str
    email: Optional[str] = None
    disabled: Optional[bool] = None

class UserInDB(User):
    hashed_password: str

# --- Auth Helper Functions ---
def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

async def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=fastapi_status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
        token_data = TokenData(username=username)
    except JWTError:
        raise credentials_exception

    user = fake_users_db.get(token_data.username)
    if user is None:
        raise credentials_exception
    return User(**user)

async def get_current_active_user(current_user: User = Depends(get_current_user)):
    if current_user.disabled:
        raise HTTPException(status_code=400, detail="Inactive user")
    return current_user

# --- FastAPI App Initialization ---
app = FastAPI(version=prog_version)

# --- Global Variables & Setup ---
# context = SessionContext() # Removed global instantiation here
context: Optional[SessionContext] = None # Define as None initially

api_base_tmp_dir = Path(tmp_dir) / "api_uploads"
api_base_audiobooks_dir = Path(audiobooks_gui_dir) / "api_outputs"
# These are fine to run at module level as they only define paths and ensure dirs exist
api_base_tmp_dir.mkdir(parents=True, exist_ok=True)
api_base_audiobooks_dir.mkdir(parents=True, exist_ok=True)

# --- Pydantic Models ---
class UploadResponse(BaseModel):
    session_id: str
    book_id: str
    filename: str
    message: str

class StatusResponse(BaseModel):
    session_id: str
    book_id: str
    status: str
    message: Optional[str] = None

class AudiobookListItem(BaseModel):
    book_id: str
    title: str
    status: str

class AudiobookListResponse(BaseModel):
    username: str
    audiobooks: List[AudiobookListItem]

# --- Helper Functions ---
def get_book_output_dir(session_id: str, book_id: str) -> Path:
    return api_base_audiobooks_dir / session_id / book_id

def get_book_upload_dir(session_id: str, book_id: str) -> Path:
    return api_base_tmp_dir / session_id / book_id

async def _generate_book_id(file: UploadFile) -> str:
    hasher = hashlib.sha256()
    await file.seek(0)
    while chunk := await file.read(8192):
        hasher.update(chunk)
    await file.seek(0)
    return hasher.hexdigest()

# --- Authentication Endpoints ---
@app.post("/auth/token", response_model=Token)
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends()):
    user = fake_users_db.get(form_data.username)
    if not user or not verify_password(form_data.password, user["hashed_password"]):
        raise HTTPException(
            status_code=fastapi_status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user["username"]}, expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer"}

# --- Protected User Endpoint ---
@app.get("/users/me", response_model=User)
async def read_users_me(current_user: User = Depends(get_current_active_user)):
    return current_user

# --- API Endpoints ---

@app.post("/upload_ebook", response_model=UploadResponse)
async def upload_ebook_endpoint(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_active_user)
):
    session_id = str(uuid.uuid4())
    original_filename = file.filename if file.filename else "untitled_ebook"
    filename_noext, _ = os.path.splitext(original_filename)

    if context is None: # Should have been initialized in __main__
        raise HTTPException(status_code=500, detail="Session context not initialized")

    try:
        book_id = await _generate_book_id(file)
        logger.info(f"Upload by user: {current_user.username}")

        upload_dir = get_book_upload_dir(session_id, book_id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        temp_ebook_path = upload_dir / original_filename

        with open(temp_ebook_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        logger.info(f"File '{original_filename}' uploaded to '{temp_ebook_path}' for session '{session_id}', book_id '{book_id}'")

        output_dir_for_book = get_book_output_dir(session_id, book_id)
        output_dir_for_book.mkdir(parents=True, exist_ok=True)

        session_data = {
            "book_id": book_id,
            "original_filename": original_filename,
            "sanitized_ebook_name": get_sanitized(filename_noext),
            "status": "PENDING",
            "upload_path": str(temp_ebook_path),
            "output_dir": str(output_dir_for_book),
            "error_message": None,
            "final_audio_path": None,
            "output_format": default_output_format,
            "owner_username": current_user.username
        }
        if session_id not in context.sessions:
            context.sessions[session_id] = {}
        context.sessions[session_id][book_id] = session_data

        args_for_conversion = { # Renamed for clarity
            "is_gui_process": False,
            "session": session_id,
            "script_mode": "API",
            "device": default_device,
            "tts_engine": default_tts_engine,
            "ebook": str(temp_ebook_path),
            "audiobooks_dir": str(output_dir_for_book),
            "voice": None,
            "language": default_language_code,
            "custom_model": None,
            "output_format": default_output_format,
            "temperature": default_xtts_settings.get('temperature', 0.75),
            "length_penalty": default_xtts_settings.get('length_penalty', 1.0),
            "fine_tuned": default_xtts_settings.get('fine_tuned', True),
            "ebook_list": None,
            "context_for_convert": context, # Pass the global context instance
            "book_id_for_convert": book_id
        }
        if default_tts_engine == "XTTS":
            args_for_conversion.update(default_xtts_settings)
        elif default_tts_engine == "BARK":
            args_for_conversion.update(default_bark_settings)

        context.sessions[session_id][book_id]["status"] = "PROCESSING"

        background_tasks.add_task(convert_ebook, args_for_conversion)
        logger.info(f"Conversion task for book_id {book_id} (session {session_id}) added to background by user {current_user.username}.")

        return UploadResponse(
            session_id=session_id,
            book_id=book_id,
            filename=original_filename,
            message="Ebook uploaded successfully. Processing started."
        )
    except Exception as e:
        logger.error(f"Error during ebook upload by {current_user.username}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to upload and process ebook: {str(e)}")
    finally:
        if file:
            try:
                file.file.close()
            except Exception:
                pass

@app.get("/audiobook_status/{session_id}/{book_id}", response_model=StatusResponse)
async def audiobook_status_endpoint(session_id: str, book_id: str, current_user: User = Depends(get_current_active_user)):
    if context is None:
        raise HTTPException(status_code=500, detail="Session context not initialized")
    session_book_data = context.sessions.get(session_id, {}).get(book_id)

    if not session_book_data or session_book_data.get("owner_username") != current_user.username:
        raise HTTPException(status_code=404, detail="Status not found or access denied for the given session_id and book_id.")

    current_status = session_book_data.get("status", "UNKNOWN")
    error_message = session_book_data.get("error_message")

    if error_message:
        return StatusResponse(session_id=session_id, book_id=book_id, status="ERROR", message=error_message)

    if current_status == "COMPLETED":
         return StatusResponse(session_id=session_id, book_id=book_id, status="COMPLETED", message="Audiobook processing completed.")

    output_dir = Path(session_book_data["output_dir"])
    audio_files = list(output_dir.glob(f"*.{session_book_data['output_format']}"))

    if audio_files:
        if current_status != "COMPLETED":
            session_book_data["status"] = "COMPLETED"
            session_book_data["final_audio_path"] = str(audio_files[0])
        return StatusResponse(session_id=session_id, book_id=book_id, status="COMPLETED", message="Audiobook is ready for download.")

    return StatusResponse(session_id=session_id, book_id=book_id, status=current_status, message="Audiobook processing is ongoing.")


@app.get("/download_audiobook/{session_id}/{book_id}")
async def download_audiobook_endpoint(session_id: str, book_id: str, current_user: User = Depends(get_current_active_user)):
    if context is None:
        raise HTTPException(status_code=500, detail="Session context not initialized")
    session_book_data = context.sessions.get(session_id, {}).get(book_id)

    if not session_book_data or session_book_data.get("owner_username") != current_user.username:
        raise HTTPException(status_code=404, detail="Audiobook not found or access denied.")

    output_dir = Path(session_book_data["output_dir"])
    output_format = session_book_data["output_format"]

    final_audio_path_from_session = session_book_data.get("final_audio_path")

    if final_audio_path_from_session and Path(final_audio_path_from_session).exists():
        audio_file_path = Path(final_audio_path_from_session)
    else:
        audio_files = list(output_dir.glob(f"*.{output_format}"))
        if not audio_files:
            # Re-check status with current_user to avoid error in status check if it's also protected
            # status_check_response = await audiobook_status_endpoint(session_id, book_id, current_user)
            # Instead of calling the endpoint, check status directly from session_book_data
            current_status_for_download = session_book_data.get("status", "UNKNOWN")
            if current_status_for_download != "COMPLETED":
                 raise HTTPException(status_code=400, detail=f"Audiobook processing not completed. Status: {current_status_for_download}")
            else:
                 logger.error(f"Audiobook for {session_id}/{book_id} (user {current_user.username}) is COMPLETED but output file not found in {output_dir}")
                 raise HTTPException(status_code=404, detail="Audiobook file not found, though processing seemed complete.")
        audio_file_path = audio_files[0]
        session_book_data["final_audio_path"] = str(audio_file_path)

    if audio_file_path.is_file():
        original_filename_no_ext = get_sanitized(Path(session_book_data["original_filename"]).stem)
        download_filename = f"{original_filename_no_ext}.{output_format}"
        media_type_map = {"mp3": "audio/mpeg", "wav": "audio/wav", "m4b": "audio/mp4"}
        media_type = media_type_map.get(output_format, "application/octet-stream")

        return FileResponse(
            path=str(audio_file_path),
            filename=download_filename,
            media_type=media_type
        )
    else:
        logger.error(f"File not found for download by {current_user.username}: {audio_file_path}")
        raise HTTPException(status_code=404, detail="Audiobook file does not exist.")

@app.get("/audiobooks", response_model=AudiobookListResponse)
async def list_my_audiobooks_endpoint(current_user: User = Depends(get_current_active_user)):
    if context is None:
        raise HTTPException(status_code=500, detail="Session context not initialized")
    user_audiobooks = []
    for sid, books_in_session in context.sessions.items():
        for bid, book_data in books_in_session.items():
            if book_data.get("owner_username") == current_user.username:
                user_audiobooks.append(AudiobookListItem(
                    book_id=bid,
                    title=book_data.get("original_filename", "Unknown Title"),
                    status=book_data.get("status", "UNKNOWN")
                ))

    return AudiobookListResponse(username=current_user.username, audiobooks=user_audiobooks)


if __name__ == "__main__":
    import uvicorn

    multiprocessing.freeze_support() # Added for multiprocessing safety

    global context # Declare context as global to modify the module-level variable
    context = SessionContext() # Instantiate SessionContext here

    logger.info(f"Starting Uvicorn server for API v{prog_version}...")
    logger.info(f"Using mock JWT: {jwt_available}, Using mock Passlib: {passlib_available}")
    logger.info(f"Temporary files will be stored under: {api_base_tmp_dir}")
    logger.info(f"Audiobook outputs will be stored under: {api_base_audiobooks_dir}")

    # Directories are already created at module level, but ensuring again doesn't hurt.
    api_base_tmp_dir.mkdir(parents=True, exist_ok=True)
    api_base_audiobooks_dir.mkdir(parents=True, exist_ok=True)

    uvicorn.run(app, host="0.0.0.0", port=8008)
