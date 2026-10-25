from fastapi import APIRouter, HTTPException
import user_models

router = APIRouter(prefix="/creation/user-models")


@router.get("")
async def list_models():
    return {
        "models": user_models.list_profiles(),
        "analysis_model_id": user_models.get_analysis_model(),
    }


@router.post("")
async def save_model(data: dict):
    try:
        return user_models.save_profile(data)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/{model_id}")
async def delete_model(model_id: str):
    try:
        user_models.delete_profile(model_id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    return {"status": "ok"}


@router.put("/analysis-selection")
async def select_analysis_model(data: dict):
    if "model_id" not in data:
        raise HTTPException(400, "缺少 model_id")
    try:
        selected = user_models.set_analysis_model(data["model_id"])
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"model_id": selected}


@router.post("/test-connection")
async def test_connection(data: dict):
    """Validate an unsaved draft; never mutate the selected or saved profiles."""
    import asyncio

    try:
        profile = user_models.prepare_profile(data)

        async def infer():
            return "".join(
                [
                    part
                    async for part in user_models.chat_configuration(
                        profile,
                        [{"role": "user", "content": "Reply with just OK."}],
                        max_tokens=256,
                    )
                ]
            )

        reply = await asyncio.wait_for(infer(), timeout=60)
        return {"status": "ok", "message": "连接成功", "reply": reply[:300]}
    except TimeoutError as exc:
        raise HTTPException(408, "连接测试超时，请检查服务或客户端状态") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            502, "连接测试失败，请检查地址、密钥和客户端登录状态"
        ) from exc


@router.get("/detect/{client}")
async def detect_local_client(client: str):
    import asyncio
    from client_detection import detect_client

    try:
        return await asyncio.to_thread(detect_client, client)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
