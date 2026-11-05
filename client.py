"""Minimal Zambo MCP client (stdlib only).

Talks JSON-RPC to https://zambo.dev/mcp. A Zambo "job" is the set of calls
that share one _session_id; journal_log records actions performed outside
Zambo (here: Hermes tool calls) into that job, and handover wraps the job
into a verifiable AER-1 receipt.
"""

import json
import urllib.request
import urllib.error

DEFAULT_URL = "https://zambo.dev/mcp"
RUN_URL = "https://zambo.dev/run/{receipt_id}"


class ZamboError(Exception):
    pass


class ZamboClient:
    def __init__(self, base_url=DEFAULT_URL, caller="hermes-agent", timeout=8.0):
        self.base_url = base_url
        self.caller = caller
        self.timeout = timeout
        self._seq = 0

    def _post(self, payload, timeout=None):
        # Prefer requests when the host environment has it: it rides through
        # proxies that truncate urllib responses. Fall back to stdlib urllib
        # with one retry so the plugin stays dependency-free.
        try:
            import requests
        except ImportError:
            requests = None
        if requests is not None:
            try:
                resp = requests.post(
                    self.base_url, json=payload, timeout=timeout or self.timeout
                )
            except Exception as e:
                raise ZamboError(f"Zambo unreachable: {e}") from e
            if resp.status_code != 200:
                raise ZamboError(f"Zambo HTTP {resp.status_code}")
            try:
                return resp.json()
            except Exception as e:
                raise ZamboError(f"Zambo returned unreadable JSON: {e}") from e
        body = json.dumps(payload).encode("utf-8")
        last_error = None
        for _attempt in range(2):
            req = urllib.request.Request(
                self.base_url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(
                    req, timeout=timeout or self.timeout
                ) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                raise ZamboError(f"Zambo HTTP {e.code}") from e
            except Exception as e:  # URLError, timeout, truncated reads
                last_error = e
        raise ZamboError(f"Zambo unreachable: {last_error}")

    def call(self, tool, arguments, session_id, timeout=None):
        """Call one Zambo tool inside a session-bound job.

        Returns the tool's structured result dict. Raises ZamboError on
        transport failure or a JSON-RPC error.
        """
        self._seq += 1
        args = dict(arguments)
        args["_session_id"] = session_id
        args["_caller_handle"] = self.caller
        data = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._seq,
                "method": "tools/call",
                "params": {"name": tool, "arguments": args},
            },
            timeout=timeout,
        )
        if "error" in data:
            raise ZamboError(f"Zambo error: {data['error']}")
        result = data.get("result") or {}
        structured = result.get("structuredContent")
        if isinstance(structured, dict) and (
            structured.get("rejection") or structured.get("executed") is False
        ):
            err = structured.get("error") or {}
            raise ZamboError(
                "Zambo rejected the call: " + str(err.get("message") or err or "unknown")
            )
        if isinstance(structured, dict):
            return structured
        for item in result.get("content") or []:
            if item.get("type") == "text":
                try:
                    parsed = json.loads(item["text"])
                except Exception:
                    continue
                if isinstance(parsed, dict):
                    return parsed
        return {}

    def receipt_id_of(self, result):
        """Pull a receipt id out of a tool result, tolerating shape drift."""
        for key in ("receipt_id", "id", "run_id"):
            value = result.get(key)
            if isinstance(value, str) and value:
                return value
        receipt = result.get("receipt")
        if isinstance(receipt, dict):
            return self.receipt_id_of(receipt)
        return None

    def journal(self, *, session_id, job_id, tool_name, metadata):
        result = self.call(
            "journal_log",
            {
                "job_id": job_id,
                "tool_name": tool_name,
                "executor_identity": self.caller,
                "external_executor": self.caller,
                "metadata": metadata,
            },
            session_id,
        )
        entry = result.get("entry") or {}
        return entry.get("entry_id") or self.receipt_id_of(result)

    def wrap(self, *, session_id, summary, goal, recipient, next_steps):
        result = self.call(
            "handover",
            {
                "summary": summary,
                "goal": goal,
                "recipient": recipient,
                "next_steps": [next_steps] if isinstance(next_steps, str) else next_steps,
            },
            session_id,
            timeout=60.0,
        )
        for key in ("workflow_receipt_url", "receipt_url"):
            url = result.get(key)
            if isinstance(url, str) and "/run/" in url:
                return url.rstrip("/").split("/run/")[-1]
        receipt_id = self.receipt_id_of(result)
        if not receipt_id:
            raise ZamboError("wrap returned no receipt id")
        return receipt_id

    def check_receipt(self, receipt_id):
        """Public verification: the receipt's verify endpoint must answer,
        and the public record at zambo.dev/run/<id> must resolve."""
        verify_url = f"https://zambo.dev/api/v2/receipt/{receipt_id}/verify"
        try:
            import requests
        except ImportError:
            requests = None
        if requests is not None:
            try:
                resp = requests.get(verify_url, timeout=self.timeout)
                if resp.status_code == 200:
                    try:
                        body = resp.json()
                    except Exception:
                        body = {}
                    return {
                        "ok": True,
                        "status": 200,
                        "url": RUN_URL.format(receipt_id=receipt_id),
                        "verify": body,
                    }
            except Exception:
                pass  # fall through to the public page check
        url = RUN_URL.format(receipt_id=receipt_id)
        try:
            import requests
        except ImportError:
            requests = None
        if requests is not None:
            try:
                resp = requests.get(url, timeout=self.timeout)
            except Exception as e:
                raise ZamboError(f"receipt check failed: {e}") from e
            return {"ok": resp.status_code == 200, "status": resp.status_code, "url": url}
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return {"ok": resp.status == 200, "status": resp.status, "url": url}
        except urllib.error.HTTPError as e:
            return {"ok": False, "status": e.code, "url": url}
        except Exception as e:
            raise ZamboError(f"receipt check failed: {e}") from e
