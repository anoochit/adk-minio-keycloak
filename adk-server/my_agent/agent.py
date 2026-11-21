# my_agent/agent.py
import io
import os
import posixpath
from datetime import timedelta
import boto3
from minio import Minio
from google.adk.agents import Agent
from google.adk.tools import ToolContext

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "localhost:9000")
MINIO_PUBLIC_ENDPOINT = os.getenv("MINIO_PUBLIC_ENDPOINT", "localhost:9000")
MINIO_USE_SSL = os.getenv("MINIO_USE_SSL", "false").lower() == "true"
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "user-files")

# MinIO's role partition is "minio" (not "aws") and the role must be the one
# MinIO generates from MINIO_IDENTITY_OPENID_ROLE_POLICY — check the startup
# line "IAM Roles: ..." in `docker compose logs minio` if STS rejects it.
MINIO_ROLE_ARN = os.getenv(
    "MINIO_ROLE_ARN", "arn:minio:iam:::role/ivd6H_TwNAxH9YkB81tz8yJe_Jk"
)


def _get_user_minio_client(tool_context: ToolContext) -> tuple[Minio, Minio, str]:
    jwt_token = tool_context.state.get("keycloak_token")
    username = tool_context.state.get("username")

    if not jwt_token or not username:
        raise ValueError("Token or user information not found. Please log in via Keycloak again.")

    scheme = "https" if MINIO_USE_SSL else "http"
    sts = boto3.client(
        "sts",
        endpoint_url=f"{scheme}://{MINIO_ENDPOINT}",
        region_name="us-east-1",
    )

    response = sts.assume_role_with_web_identity(
        RoleArn=MINIO_ROLE_ARN,
        RoleSessionName=f"adk-{username}"[:64],
        WebIdentityToken=jwt_token,
        DurationSeconds=900,
    )
    creds = response["Credentials"]

    internal_client = Minio(
        MINIO_ENDPOINT,
        access_key=creds["AccessKeyId"],
        secret_key=creds["SecretAccessKey"],
        session_token=creds["SessionToken"],
        secure=MINIO_USE_SSL,
    )

    public_client = Minio(
        MINIO_PUBLIC_ENDPOINT,
        access_key=creds["AccessKeyId"],
        secret_key=creds["SecretAccessKey"],
        session_token=creds["SessionToken"],
        region="us-east-1",
        secure=MINIO_USE_SSL,
    )

    return internal_client, public_client, username


def _safe_user_path(username: str, filename: str) -> str:
    """Force path to stay under {username}/ only and prevent path traversal."""
    safe_user = posixpath.basename(username.strip())
    if not safe_user or safe_user in (".", ".."):
        raise ValueError("Invalid username")

    raw = filename.strip().lstrip("/")
    if raw.startswith(f"{safe_user}/"):
        raw = raw[len(safe_user) + 1 :]

    normalized = posixpath.normpath(raw)
    if (
        not normalized
        or normalized in (".", "..")
        or normalized.startswith("../")
        or "/../" in f"/{normalized}/"
    ):
        raise ValueError("Invalid file path")

    return f"{safe_user}/{normalized}"


def list_my_files(tool_context: ToolContext) -> str:
    try:
        client, _, username = _get_user_minio_client(tool_context)
        prefix = f"{username}/"
        objects = client.list_objects(MINIO_BUCKET, prefix=prefix, recursive=True)
        files = [
            f"- {obj.object_name.removeprefix(prefix)} ({obj.size} bytes)"
            for obj in objects
            if obj.object_name.removeprefix(prefix)
        ]
        return "\n".join(files) if files else "Your folder does not contain any files."
    except Exception as e:
        return f"Error listing files: {str(e)}"


def upload_text_file(filename: str, content: str, tool_context: ToolContext) -> str:
    try:
        client, _, username = _get_user_minio_client(tool_context)
        object_name = _safe_user_path(username, filename)
        data = content.encode("utf-8")
        client.put_object(
            bucket_name=MINIO_BUCKET,
            object_name=object_name,
            data=io.BytesIO(data),
            length=len(data),
            content_type="text/plain; charset=utf-8",
        )
        return f"File '{filename}' saved successfully."
    except Exception as e:
        return f"Failed to upload file '{filename}': {str(e)}"


def read_text_file(filename: str, tool_context: ToolContext) -> str:
    try:
        client, _, username = _get_user_minio_client(tool_context)
        object_name = _safe_user_path(username, filename)
        response = client.get_object(MINIO_BUCKET, object_name)
        try:
            return response.read().decode("utf-8")
        finally:
            response.close()
            response.release_conn()
    except Exception as e:
        return f"Could not read file '{filename}': {str(e)}"


def delete_file(filename: str, tool_context: ToolContext) -> str:
    try:
        client, _, username = _get_user_minio_client(tool_context)
        object_name = _safe_user_path(username, filename)
        try:
            client.stat_object(MINIO_BUCKET, object_name)
        except Exception as stat_err:
            if "NoSuchKey" in str(stat_err):
                return f"File '{filename}' not found in your folder."
            raise stat_err
        client.remove_object(MINIO_BUCKET, object_name)
        return f"File '{filename}' deleted successfully."
    except Exception as e:
        return f"Failed to delete file '{filename}': {str(e)}"


def get_download_link(filename: str, tool_context: ToolContext) -> str:
    try:
        _, public_client, username = _get_user_minio_client(tool_context)
        object_name = _safe_user_path(username, filename)
        url = public_client.presigned_get_object(
            bucket_name=MINIO_BUCKET,
            object_name=object_name,
            expires=timedelta(minutes=10),
        )
        return f"Download link for '{filename}' (valid for 10 minutes): {url}"
    except Exception as e:
        return f"Failed to generate download link: {str(e)}"


def get_upload_link(filename: str, tool_context: ToolContext) -> str:
    try:
        _, public_client, username = _get_user_minio_client(tool_context)
        object_name = _safe_user_path(username, filename)
        url = public_client.presigned_put_object(
            bucket_name=MINIO_BUCKET,
            object_name=object_name,
            expires=timedelta(minutes=10),
        )
        return f"Presigned upload URL for '{filename}': {url}"
    except Exception as e:
        return f"Failed to generate upload link: {str(e)}"


root_agent = Agent(
    name="minio_assistant",
    model="gemini-3.5-flash",
    description="Personal file management assistant on MinIO",
    instruction=(
        "You are a personal file management assistant on object storage. "
        "Use the available tools to list files, read files, upload files, delete files, or generate download/upload links. "
        "Never attempt to access files outside of the user's folder. "
        "Always output your responses in plain text only. Do not use any markdown formatting or markdown styling (such as asterisks for bold or italics, markdown headings, bullet list markers, code blocks, backticks, or markdown links)."
    ),
    tools=[
        list_my_files,
        upload_text_file,
        read_text_file,
        delete_file,
        get_download_link,
        get_upload_link,
    ],
)