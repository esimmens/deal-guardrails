from fastapi import APIRouter, Depends

from ..config import get_settings
from ..outbox import dispatch_once
from ..security import verify_hmac

router = APIRouter()


@router.post("/notifications/dispatch", dependencies=[Depends(verify_hmac)])
def dispatch():
    return dispatch_once(get_settings())
