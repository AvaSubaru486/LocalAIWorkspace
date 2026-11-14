"""Exercise the running HTTP service, including a real interrupted generation."""
import json
import pathlib
import time

import httpx


def main():
    base = "http://127.0.0.1:39200"
    with httpx.Client(timeout=180) as client:
        bootstrap = client.get(base + "/api/bootstrap").json()
        client.headers["x-workspace-token"] = bootstrap["token"]
        session = client.post(base + "/api/sessions", json={}).json()["id"]
        def chat(message):
            events = []
            with client.stream("POST", base + "/chat", json={"session_id": session, "message": message, "mode": "goal"}) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if line.startswith("data: "):
                        event = json.loads(line[6:])
                        events.append(event)
                        if event["type"] == "error":
                            raise AssertionError(event)
            return events
        first = chat("Remember the password word cedar. Reply only OK.")
        assert any(e.get("available") for e in first if e["type"] == "auxiliary"), first
        second = chat("What is the password word? Reply with one word.")
        answer = "".join(e.get("text", "") for e in second)
        assert "cedar" in answer.lower(), answer
        run = client.post(base + "/api/python/run", json={"session_id": session, "code": "print('你好，Python')", "user_initiated": True}).json()
        assert run["returncode"] == 0 and "你好，Python" in run["stdout"], run
        with client.stream("POST", base + "/chat", json={"session_id": session, "message": "Write a long numbered list of 200 facts about forests."}) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if line.startswith("data: ") and json.loads(line[6:])["type"] == "delta":
                    break
        deadline = time.monotonic() + 15
        released = False
        while time.monotonic() < deadline:
            response = client.post(base + "/api/models/unload", json={})
            if response.status_code == 200:
                released = True
                break
            time.sleep(.2)
        assert released, "cancelled generation held the model lock"
        history = client.get(base + f"/api/sessions/{session}").json()
        assert any("回答中断" in m["content"] for m in history["messages"]), history
        assert client.delete(base + f"/api/sessions/{session}").status_code == 200
        print(json.dumps({"passed": True, "streaming": True, "auxiliary": True, "history_answer": answer,
                          "python_utf8": True, "cancellation_releases_model": True, "session_cleanup": True}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
