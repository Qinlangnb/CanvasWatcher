import pytest

from app.services.canvas_lti import gradescope_launch


@pytest.mark.parametrize("scenario", ["ok", "ambiguous", "wrong_host", "permission", "unrelated"])
async def test_gradescope_lti_is_optional_bounded_and_same_origin(scenario):
    calls = []
    class Client:
        base_url = "https://canvas.example"
        async def get_all_pages(self, path, params):
            calls.append(path)
            if scenario == "permission":
                raise ValueError("not permitted")
            tools = [{"id": 23, "name": "Gradescope", "url": "https://www.gradescope.com/lti/launch",
                      "course_navigation": {"enabled": True}}]
            if scenario == "ambiguous":
                tools.append({**tools[0], "id": 24})
            if scenario == "unrelated":
                tools[0]["url"] = "https://www.gradescope.com.attacker.example/launch"
            return tools
        async def get_json(self, path, params):
            calls.append(path)
            assert dict(params) == {"id": "23", "launch_type": "course_navigation"}
            return {"url": "https://attacker.example/" if scenario == "wrong_host" else "https://canvas.example/opaque-launch"}
    result = await gradescope_launch(Client(), "123", "https://www.gradescope.com")
    assert result == ("https://canvas.example/opaque-launch" if scenario == "ok" else None)
    assert len(calls) <= 2
