import logging
import os
import re
from pathlib import Path

import boto3
from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from ..utils.auth import require_perm

logger = logging.getLogger(__name__)

_ALLOWED_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
_SAFE_FILENAME_RE = re.compile(r"^[\w\-. ]+$")


@csrf_exempt
def get_presigned_url(request):
    forbidden = require_perm(request, "chatbot.add_avatar")
    if forbidden:
        return forbidden

    file_name = request.GET.get("filename", "")
    content_type = request.GET.get("content_type", "")

    if not file_name or not content_type:
        return JsonResponse(
            {"error": "filename and content_type are required."}, status=400
        )

    if content_type not in _ALLOWED_IMAGE_CONTENT_TYPES:
        return JsonResponse({"error": "Unsupported content type."}, status=415)

    # Sanitize filename: strip directory traversal and allow only safe characters
    safe_name = Path(file_name).name
    if not _SAFE_FILENAME_RE.match(safe_name):
        return JsonResponse({"error": "Invalid filename."}, status=400)

    try:
        if settings.BACKEND_ENVIRONMENT == "local":
            s3_client = boto3.client(
                "s3",
                aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
                aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
                region_name=os.getenv("AWS_REGION"),
            )
        else:
            s3_client = boto3.client(
                "s3",
                region_name=os.getenv("AWS_REGION"),
            )

        url = s3_client.generate_presigned_url(
            ClientMethod="put_object",
            Params={
                "Bucket": os.getenv("AWS_BUCKET_NAME"),
                "Key": f"uploads/{safe_name}",
                "ContentType": content_type,
            },
            ExpiresIn=300,
            HttpMethod="PUT",
        )

        return JsonResponse(
            {
                "s3_url": url,
                "file_url": f"https://{os.getenv('AWS_BUCKET_NAME')}.s3.amazonaws.com/uploads/{safe_name}",
            },
            status=200,
        )
    except Exception as e:
        logger.error(f"Error generating presigned URL: {e}")
        return JsonResponse({"error": "Failed to generate upload URL."}, status=500)
