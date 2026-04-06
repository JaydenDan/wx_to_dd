import uvicorn
from src.config import global_config
from src.app import app


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=global_config.VXHOOK_CALLBACK_HOST,
        port=global_config.VXHOOK_CALLBACK_PORT,
        log_config=None,
    )
