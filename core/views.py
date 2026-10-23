import json

from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .services import (
    LocationNotFoundError,
    NoFuelStationsError,
    RouteServiceError,
    plan_route,
)


@csrf_exempt
@require_POST
def route_api(request: HttpRequest) -> JsonResponse:
    try:
        payload = json.loads(request.body)
        print(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "Request body must be valid JSON."}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"error": "Request body must be a JSON object."}, status=400)
    start = payload.get("start")
    finish = payload.get("finish")
    if not isinstance(start, str) or not start.strip():
        return JsonResponse(
            {"error": "'start' must be a non-empty location string."}, status=400
        )
    if not isinstance(finish, str) or not finish.strip():
        return JsonResponse(
            {"error": "'finish' must be a non-empty location string."}, status=400
        )
    if len(start) > 200 or len(finish) > 200:
        return JsonResponse({"error": "Locations must be 200 characters or fewer."}, status=400)
    try:
        return JsonResponse(plan_route(start.strip(), finish.strip()))
    except LocationNotFoundError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except NoFuelStationsError as exc:
        return JsonResponse({"error": str(exc)}, status=422)
    except RouteServiceError as exc:
        return JsonResponse({"error": str(exc)}, status=502)
