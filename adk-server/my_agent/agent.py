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
MINIO_USE_SSL = os.getenv("MINIO_USE_SSL", "false").lower() == "true"
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "user-files")
# MinIO's role partition is "minio" (not "aws") and the role must be the one
# MinIO generates from MINIO_IDENTITY_OPENID_ROLE_POLICY — check the startup
# line "IAM Roles: ..." in `docker compose logs minio` if STS rejects it.
MINIO_ROLE_ARN = os.getenv(
    "MINIO_ROLE_ARN", "arn:minio:iam:::role/ivd6H_TwNAxH9YkB81tz8yJe_Jk"
)


def _get_user_minio_client(tool_context: ToolContext) -> tuple[Minio, str]:
    jwt_token = tool_context.state.get("keycloak_token")
    username = tool_context.state.get("username")

    if not jwt_token or not username:
        raise ValueError("ไม่พบ Token หรือข้อมูลผู้ใช้ กรุณาล็อกอินผ่าน Keycloak ใหม่")

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

    client = Minio(
        MINIO_ENDPOINT,
        access_key=creds["AccessKeyId"],
        secret_key=creds["SecretAccessKey"],
        session_token=creds["SessionToken"],
        secure=MINIO_USE_SSL,
    )
    return client, username


def _safe_user_path(username: str, filename: str) -> str:
    """บังคับให้อยู่ใต้โฟลเดอร์ {username}/ เท่านั้น และกัน path traversal"""
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
        client, username = _get_user_minio_client(tool_context)
        prefix = f"{username}/"
        objects = client.list_objects(MINIO_BUCKET, prefix=prefix, recursive=True)
        files = [
            f"- {obj.object_name.removeprefix(prefix)} ({obj.size} bytes)"
            for obj in objects
            if obj.object_name.removeprefix(prefix)
        ]
        return "\n".join(files) if files else "โฟลเดอร์ของคุณยังไม่มีไฟล์"
    except Exception as e:
        return f"เกิดข้อผิดพลาดในการดึงรายชื่อไฟล์: {str(e)}"


def upload_text_file(filename: str, content: str, tool_context: ToolContext) -> str:
    try:
        client, username = _get_user_minio_client(tool_context)
        object_name = _safe_user_path(username, filename)
        data = content.encode("utf-8")
        client.put_object(
            bucket_name=MINIO_BUCKET,
            object_name=object_name,
            data=io.BytesIO(data),
            length=len(data),
            content_type="text/plain; charset=utf-8",
        )
        return f"บันทึกไฟล์ '{filename}' เรียบร้อยแล้ว"
    except Exception as e:
        return f"อัปโหลดไฟล์ไม่สำเร็จ: {str(e)}"


def read_text_file(filename: str, tool_context: ToolContext) -> str:
    try:
        client, username = _get_user_minio_client(tool_context)
        object_name = _safe_user_path(username, filename)
        response = client.get_object(MINIO_BUCKET, object_name)
        try:
            return response.read().decode("utf-8")
        finally:
            response.close()
            response.release_conn()
    except Exception as e:
        return f"ไม่สามารถอ่านไฟล์ '{filename}' ได้: {str(e)}"


def get_download_link(filename: str, tool_context: ToolContext) -> str:
    try:
        client, username = _get_user_minio_client(tool_context)
        object_name = _safe_user_path(username, filename)
        url = client.presigned_get_object(
            bucket_name=MINIO_BUCKET,
            object_name=object_name,
            expires=timedelta(minutes=10),
        )
        return f"ลิงก์ดาวน์โหลดสำหรับ '{filename}' (ใช้ได้ 10 นาที): {url}"
    except Exception as e:
        return f"สร้างลิงก์ดาวน์โหลดไม่สำเร็จ: {str(e)}"


def get_upload_link(filename: str, tool_context: ToolContext) -> str:
    try:
        client, username = _get_user_minio_client(tool_context)
        object_name = _safe_user_path(username, filename)
        url = client.presigned_put_object(
            bucket_name=MINIO_BUCKET,
            object_name=object_name,
            expires=timedelta(minutes=10),
        )
        return f"Presigned Upload URL สำหรับ '{filename}': {url}"
    except Exception as e:
        return f"สร้างลิงก์อัปโหลดไม่สำเร็จ: {str(e)}"


root_agent = Agent(
    name="minio_assistant",
    model="gemini-2.5-flash",
    description="ผู้ช่วยจัดการไฟล์ส่วนตัวของผู้ใช้บน MinIO",
    instruction=(
        "คุณคือผู้ช่วยจัดการไฟล์ส่วนตัวของผู้ใช้บนระบบจัดเก็บข้อมูล "
        "ใช้เครื่องมือที่มีเพื่อดูรายชื่อไฟล์ อ่านไฟล์ สร้างไฟล์ หรือสร้างลิงก์ดาวน์โหลด/อัปโหลด "
        "ห้ามพยายามเข้าถึงไฟล์นอกโฟลเดอร์ของผู้ใช้"
    ),
    tools=[
        list_my_files,
        upload_text_file,
        read_text_file,
        get_download_link,
        get_upload_link,
    ],
)