"""A small async client for the Docker Engine API, over the daemon's unix socket."""

import json
import struct
from typing import Any, cast

import httpx

API_VERSION = "v1.44"
DEFAULT_SOCKET = "/var/run/docker.sock"


class DockerError(Exception):
    """The daemon refused a request, or answered with an error."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"docker answered {status}: {message}")
        self.status = status


def _demux(raw: bytes) -> str:
    """Join the frames of a non-tty log stream: each is an 8-byte header and its payload."""
    out = bytearray()
    at = 0
    while at + 8 <= len(raw):
        (size,) = struct.unpack(">I", raw[at + 4 : at + 8])
        out += raw[at + 8 : at + 8 + size]
        at += 8 + size
    return out.decode(errors="replace")


class Docker:
    def __init__(self, socket_path: str = DEFAULT_SOCKET) -> None:
        self._http = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=socket_path),
            base_url=f"http://docker/{API_VERSION}",
            timeout=httpx.Timeout(60.0),
        )

    async def _call(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        seconds: float | None = 60.0,
        allow: tuple[int, ...] = (),
    ) -> httpx.Response:
        response = await self._http.request(method, path, json=body, params=params, timeout=seconds)
        if response.status_code >= 400 and response.status_code not in allow:
            try:
                message = str(response.json().get("message", response.text))
            except ValueError:
                message = response.text
            raise DockerError(response.status_code, message)
        return response

    async def ping(self) -> bool:
        try:
            return (await self._call("GET", "/_ping")).text == "OK"
        except (httpx.TransportError, DockerError):
            return False

    async def image(self, image: str) -> dict[str, Any] | None:
        """What the daemon knows about an image, or None when it doesn't have it."""
        response = await self._call("GET", f"/images/{image}/json", allow=(404,))
        if response.status_code == 404:
            return None
        return cast(dict[str, Any], response.json())

    async def image_id(self, image: str) -> str | None:
        """The image's content digest, or None when the daemon doesn't have it."""
        found = await self.image(image)
        return None if found is None else str(found["Id"])

    async def create(self, name: str, config: dict[str, Any]) -> str:
        response = await self._call(
            "POST", "/containers/create", body=config, params={"name": name}
        )
        return str(response.json()["Id"])

    async def start(self, container: str) -> None:
        await self._call("POST", f"/containers/{container}/start")

    async def wait(self, container: str) -> int:
        """Block until the container stops, and return its exit code."""
        response = await self._call("POST", f"/containers/{container}/wait", seconds=None)
        return int(response.json()["StatusCode"])

    async def inspect(self, container: str) -> dict[str, Any]:
        return cast(
            dict[str, Any], (await self._call("GET", f"/containers/{container}/json")).json()
        )

    async def logs(self, container: str, *, tail: int = 200) -> str:
        response = await self._call(
            "GET",
            f"/containers/{container}/logs",
            params={"stdout": "1", "stderr": "1", "tail": str(tail)},
        )
        return _demux(response.content)

    async def kill(self, container: str) -> None:
        # 409: it had already stopped, which is what a kill wants anyway
        await self._call("POST", f"/containers/{container}/kill", allow=(404, 409))

    async def remove(self, container: str) -> None:
        await self._call(
            "DELETE", f"/containers/{container}", params={"force": "true"}, allow=(404, 409)
        )

    async def containers(self, labels: dict[str, str]) -> list[dict[str, Any]]:
        """Every container, running or not, that carries all of these labels."""
        filters = json.dumps({"label": [f"{key}={value}" for key, value in labels.items()]})
        response = await self._call(
            "GET", "/containers/json", params={"all": "true", "filters": filters}
        )
        return cast(list[dict[str, Any]], response.json())

    async def stats(self, container: str) -> dict[str, Any]:
        response = await self._call(
            "GET", f"/containers/{container}/stats", params={"stream": "false"}
        )
        return cast(dict[str, Any], response.json())

    async def create_network(self, name: str, *, internal: bool, labels: dict[str, str]) -> str:
        body = {"Name": name, "Driver": "bridge", "Internal": internal, "Labels": labels}
        return str((await self._call("POST", "/networks/create", body=body)).json()["Id"])

    async def connect(self, network: str, container: str, *, aliases: list[str]) -> None:
        body = {"Container": container, "EndpointConfig": {"Aliases": aliases}}
        await self._call("POST", f"/networks/{network}/connect", body=body)

    async def remove_network(self, network: str) -> None:
        await self._call("DELETE", f"/networks/{network}", allow=(404,))

    async def networks(self, labels: dict[str, str]) -> list[dict[str, Any]]:
        filters = json.dumps({"label": [f"{key}={value}" for key, value in labels.items()]})
        response = await self._call("GET", "/networks", params={"filters": filters})
        return cast(list[dict[str, Any]], response.json())

    async def aclose(self) -> None:
        await self._http.aclose()
