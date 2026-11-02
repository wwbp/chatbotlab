from django.http import JsonResponse


def require_perm(request, perm):
    """Return a 403 JsonResponse unless the user is staff with `perm`, else None.

    Staff alone is not enough: Researchers are staff (they use the admin) but
    must not edit bots. See docs/deployment/access-control.rst for the roles.
    """
    if not (request.user.is_staff and request.user.has_perm(perm)):
        return JsonResponse({"error": "Forbidden."}, status=403)
    return None
