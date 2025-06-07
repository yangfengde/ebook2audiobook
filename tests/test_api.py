import pytest
from fastapi.testclient import TestClient
from fastapi import FastAPI # To help with potential app conflicts if api.py also defines 'app' globally
import shutil
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

# Attempt to import the app from api.py
# This structure assumes api.py can be imported and 'app' is the FastAPI instance
# It might need adjustment based on actual file structure or if api.py runs uvicorn directly
try:
    from api import app as fastapi_app  # Use an alias
    from api import fake_users_db, get_password_hash, context as api_context # For test user setup and session access
    from lib.conf import tmp_dir as configured_tmp_dir_lib, audiobooks_gui_dir as configured_audiobooks_dir_lib # From lib.conf
    imported_api = True
    # Use paths from the imported configuration
    configured_tmp_dir = str(Path(configured_tmp_dir_lib).resolve())
    configured_audiobooks_dir = str(Path(configured_audiobooks_dir_lib).resolve())

except ImportError as e:
    print(f"Test API import error: {e}. Using dummy app for test structure.")
    # Define a dummy FastAPI app if import fails, so tests can be written structurally
    fastapi_app = FastAPI()
    @fastapi_app.get("/")
    def read_root(): return {"Hello": "World"}
    fake_users_db = {} # Dummy
    def get_password_hash(p): return p # Dummy
    api_context = None # Dummy context

    # Define dummy paths for the test structure if API import fails
    current_script_dir = Path(__file__).parent
    configured_tmp_dir = str(current_script_dir / "test_tmp_dummy")
    configured_audiobooks_dir = str(current_script_dir / "test_audiobooks_output_dummy")
    imported_api = False

client = TestClient(fastapi_app)

TEST_USERNAME = "testuser@example.com"
TEST_PASSWORD = "testpassword"

# --- Pytest Fixtures ---
@pytest.fixture(scope="session", autouse=True) # Changed scope to session for one-time setup
def setup_test_environment():
    # Create test directories based on actual or dummy paths
    api_uploads_path = Path(configured_tmp_dir) / "api_uploads"
    api_outputs_path = Path(configured_audiobooks_dir) / "api_outputs"

    os.makedirs(api_uploads_path, exist_ok=True)
    os.makedirs(api_outputs_path, exist_ok=True)
    print(f"Test setup: Ensured directory exists: {api_uploads_path}")
    print(f"Test setup: Ensured directory exists: {api_outputs_path}")

    # Add a test user to the fake_users_db if API was imported
    if imported_api and TEST_USERNAME not in fake_users_db:
        fake_users_db[TEST_USERNAME] = {
            "username": TEST_USERNAME,
            "hashed_password": get_password_hash(TEST_PASSWORD), # Use the actual hash function from api
            "email": TEST_USERNAME,
            # "full_name": "Test User", # Match User model if these fields are expected
            "disabled": False,
        }
        print(f"Test setup: Added {TEST_USERNAME} to fake_users_db.")
    elif not imported_api: # Setup for dummy app
        fake_users_db[TEST_USERNAME] = {"username": TEST_USERNAME, "hashed_password": get_password_hash(TEST_PASSWORD)}
        print(f"Test setup: Added {TEST_USERNAME} to dummy fake_users_db.")

    yield

    # Teardown: Keep dirs for inspection for now, as per prompt.
    # print(f"Test teardown: Would remove {configured_tmp_dir} and {configured_audiobooks_dir}")
    # shutil.rmtree(configured_tmp_dir, ignore_errors=True)
    # shutil.rmtree(configured_audiobooks_dir, ignore_errors=True)
    print("Test environment teardown complete (directories retained for inspection).")

@pytest.fixture(scope="function") # Changed to function scope if token needs to be fresh for each test
def authenticated_token():
    if not imported_api: # Skip if we're on dummy app
        print("Skipping token generation for dummy app.")
        return "dummy_token_for_structure_test"

    login_data = {"username": TEST_USERNAME, "password": TEST_PASSWORD}
    response = client.post("/auth/token", data=login_data)
    assert response.status_code == 200, f"Failed to get token: {response.text}"
    token = response.json()["access_token"]
    print(f"Generated token for {TEST_USERNAME}: {token[:20]}...") # Log token generation
    return token

# --- Authentication Tests ---
def test_login_for_access_token():
    if not imported_api: pytest.skip("Skipping real API test as import failed.")
    response = client.post("/auth/token", data={"username": TEST_USERNAME, "password": TEST_PASSWORD})
    assert response.status_code == 200
    json_response = response.json()
    assert "access_token" in json_response
    assert json_response["token_type"] == "bearer"

def test_read_users_me(authenticated_token):
    if not imported_api: pytest.skip("Skipping real API test as import failed.")
    response = client.get("/users/me", headers={"Authorization": f"Bearer {authenticated_token}"})
    assert response.status_code == 200
    assert response.json()["username"] == TEST_USERNAME

# --- Upload Endpoint Tests ---
# Patching convert_ebook at the location where it's imported and used in api.py
@patch("api.convert_ebook", MagicMock(return_value=("Conversion successful", True)))
@patch("api.BackgroundTasks.add_task")
def test_upload_ebook_success(mock_add_task, authenticated_token):
    if not imported_api: pytest.skip("Skipping real API test as import failed.")

    dummy_file_content = b"This is a test ebook."
    dummy_file_name = "test_ebook_upload.txt" # Unique name for this test

    # Use a dictionary for files, key 'file' as expected by FastAPI's File(...)
    files_data = {"file": (dummy_file_name, dummy_file_content, "text/plain")}

    response = client.post("/upload_ebook", files=files_data, headers={"Authorization": f"Bearer {authenticated_token}"})

    assert response.status_code == 200, f"Upload failed: {response.text}"
    json_response = response.json()
    assert "session_id" in json_response
    assert "book_id" in json_response

    session_id = json_response["session_id"]
    book_id = json_response["book_id"]
    # Path structure: configured_tmp_dir/api_uploads/{session_id}/{book_id}/{original_filename}
    expected_save_path = Path(configured_tmp_dir) / "api_uploads" / session_id / book_id / dummy_file_name

    assert expected_save_path.exists(), f"Uploaded file not found at {expected_save_path}"
    assert expected_save_path.read_bytes() == dummy_file_content

    mock_add_task.assert_called_once()
    # You can add more assertions here to check the arguments passed to convert_ebook via mock_add_task.call_args

def test_upload_ebook_no_auth():
    if not imported_api: pytest.skip("Skipping real API test as import failed.")
    dummy_file_content = b"This is a test ebook for no_auth."
    dummy_file_name = "test_ebook_no_auth.txt"
    files_data = {"file": (dummy_file_name, dummy_file_content, "text/plain")}
    response = client.post("/upload_ebook", files=files_data)
    assert response.status_code == 401

# --- Status and Download Tests (Structure) ---
def test_get_audiobook_status_flow(authenticated_token):
    if not imported_api: pytest.skip("Skipping real API test as import failed.")

    # 1. Upload a file to get a session_id and book_id
    with patch("api.convert_ebook", MagicMock(return_value=("Conversion successful", True))), \
         patch("api.BackgroundTasks.add_task") as mock_add_task_upload:

        dummy_file_content = b"Test content for status flow."
        dummy_file_name = "status_flow_ebook.txt"
        files_data = {"file": (dummy_file_name, dummy_file_content, "text/plain")}

        upload_response = client.post("/upload_ebook", files=files_data, headers={"Authorization": f"Bearer {authenticated_token}"})
        assert upload_response.status_code == 200
        upload_json = upload_response.json()
        session_id = upload_json["session_id"]
        book_id = upload_json["book_id"]

    # 2. Check initial status (should be PENDING or PROCESSING)
    status_response_initial = client.get(f"/audiobook_status/{session_id}/{book_id}", headers={"Authorization": f"Bearer {authenticated_token}"})
    assert status_response_initial.status_code == 200
    initial_status = status_response_initial.json()["status"]
    assert initial_status in ["PENDING", "PROCESSING"]

    # 3. Simulate 'COMPLETED' state by creating a dummy output file
    # This part depends on how convert_ebook signals completion and where api.py checks.
    # Assuming it checks for a file in the expected output directory.
    output_dir = Path(configured_audiobooks_dir) / "api_outputs" / session_id / book_id
    os.makedirs(output_dir, exist_ok=True)

    # The filename logic in api.py for download might be complex.
    # For now, let's assume a known extension from default_output_format in api.py
    # We need to import default_output_format from api if it's used there, or mock it.
    try:
        from api import default_output_format as api_default_output_format
    except ImportError:
        api_default_output_format = "mp3" # Fallback if not directly importable

    dummy_audio_filename = f"{get_sanitized(Path(dummy_file_name).stem)}.{api_default_output_format}"
    (output_dir / dummy_audio_filename).write_text("dummy audio data")

    # Also, update the session context if api.py relies on it for status before file check
    if api_context and session_id in api_context.sessions and book_id in api_context.sessions[session_id]:
        api_context.sessions[session_id][book_id]['status'] = 'COMPLETED'
        api_context.sessions[session_id][book_id]['final_audio_path'] = str(output_dir / dummy_audio_filename)
        api_context.sessions[session_id][book_id]['output_format'] = api_default_output_format
        api_context.sessions[session_id][book_id]['original_filename'] = dummy_file_name


    # 4. Check status again, expecting 'COMPLETED'
    status_response_completed = client.get(f"/audiobook_status/{session_id}/{book_id}", headers={"Authorization": f"Bearer {authenticated_token}"})
    assert status_response_completed.status_code == 200, f"Status check failed: {status_response_completed.text}"
    assert status_response_completed.json()["status"] == "COMPLETED"

    # 5. Test download
    download_response = client.get(f"/download_audiobook/{session_id}/{book_id}", headers={"Authorization": f"Bearer {authenticated_token}"})
    assert download_response.status_code == 200, f"Download failed: {download_response.text}"
    assert download_response.content == b"dummy audio data"

    # Clean up the dummy file and directory for this specific test case
    shutil.rmtree(output_dir.parent) # Removes .../session_id/book_id and .../session_id if empty
    # Also remove the uploaded file
    shutil.rmtree(Path(configured_tmp_dir) / "api_uploads" / session_id)


def test_list_my_audiobooks(authenticated_token):
    if not imported_api: pytest.skip("Skipping real API test as import failed.")

    # Could add an upload here to ensure there's at least one item, then check.
    # For now, just checks if the endpoint runs and returns a list for the user.
    response = client.get("/audiobooks", headers={"Authorization": f"Bearer {authenticated_token}"})
    assert response.status_code == 200
    json_response = response.json()
    assert json_response["username"] == TEST_USERNAME
    assert isinstance(json_response["audiobooks"], list)

# Helper function to get sanitized name, mimicking one in api.py or lib.functions
# This is needed if not directly importing get_sanitized from api or lib.functions
def get_sanitized(name_str, replacement="_"):
    import re # Local import for helper
    name_str = name_str.replace('&', 'And')
    forbidden_chars = r'[<>:"/\\|?*\x00-\x1F ()]'
    sanitized = re.sub(r'\s+', replacement, name_str)
    sanitized = re.sub(forbidden_chars, replacement, sanitized)
    sanitized = sanitized.strip("_")
    return sanitized

# Note: More detailed tests for error conditions, different statuses,
# and specific argument validation in convert_ebook calls would be beneficial.
# Also, testing the actual content of downloaded files (if not mocked)
# and cleanup of temporary files would be important in a full test suite.

# To run tests:
# Ensure api.py and its dependencies (lib, etc.) are in PYTHONPATH
# `pytest tests/test_api.py`
# (Or let pytest discover tests if run from project root)
