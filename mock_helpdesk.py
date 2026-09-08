#!/usr/bin/env python3
"""The two systems an insurance operator touches during a call — simulated.

Simplified from the five-service harness in the `handoff_test` project
(`mock_servers.py`): the same stdlib `http.server` dispatch with
`{placeholder}` path matching, reduced to what one phone call needs —
find the caller's policy, open a claim file. Everything that harness
carries for an underwriting pipeline (SINCO, FIVA, KYC, AML,
antifraude, ports per service) is gone.

WHY THIS EXISTS. Without it the agent is a helpdesk that cannot look
anything up, so every call ends the same way: "no puedo consultarlo
desde aquí". That is not a demonstration of a helpdesk, it is a
demonstration of one that is broken. With it, the agent locates the
policy, takes the parte, and gives back an expediente number — which is
what the call is FOR.

THE DATA IS INVENTED and deliberately obvious about it: policies are
`LD-2026-…`, and the DNIs are not valid Spanish DNIs (the checksum
letter is wrong on purpose) so nothing here can be mistaken for a real
person's. Nothing is persisted; an expediente number is minted per
process and forgotten on exit.

    python mock_helpdesk.py            # port 8731, or $JAATO_HELPDESK_PORT
"""
import json
import os
import random
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get("JAATO_HELPDESK_PORT", "8731"))

#: The demo customers.  Say one of these on the phone and the agent
#: finds it; say anything else and it correctly reports no encontrarla,
#: which is worth showing too -- an operator who "finds" every policy is
#: not demonstrating a lookup.
POLIZAS = [
    {"poliza": "LD-2026-004417", "titular": "Daniel Alonso Gázquez",
     "nombre": "Daniel", "telefono": "615071573",
     "dni": "51234567A", "matricula": "4417-KDN",
     "vehiculo": "Seat León 1.5 TSI (2021)",
     "cobertura": "todo_riesgo", "franquicia": 300.0, "alta": "2021-03-14"},
    {"poliza": "LD-2026-008842", "titular": "María Serrano Gil",
     "nombre": "María", "telefono": "622334455",
     "dni": "52345678B", "matricula": "8842-LPT",
     "vehiculo": "Renault Clio 1.0 TCe (2019)",
     "cobertura": "terceros_ampliado", "franquicia": 0.0, "alta": "2019-09-02"},
]


def _phone(value: str) -> str:
    """A Spanish mobile, however it was written or dictated.

    Drops spaces and punctuation, then the country code: a call can
    present as `615071573`, `615 07 15 73` or `+34 615 071 573` and they
    are one number. Comparing them literally is how a line fails to
    recognise its own customer.
    """
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    return digits[2:] if digits.startswith("34") and len(digits) > 9 else digits


def _norm(value: str) -> str:
    """Compare policy and DNI loosely, because these arrive by VOICE.

    A number read aloud and typed back by a model comes with spaces,
    dots or hyphens wherever it heard a pause.  Matching on the bare
    alphanumerics is not laxity -- it is the difference between a demo
    that works when spoken and one that only works when pasted.
    """
    return "".join(ch for ch in (value or "") if ch.isalnum()).upper()


def buscar_poliza(query, body):
    """GET /v1/polizas?poliza=…&dni=…&matricula=… — any key locates it.

    FOUR keys, and the first one is the one that matters: a call arrives
    with a number attached, so a real insurance line knows who is
    calling before it says hello.  The reference recording opens with
    "para tu coche Fiat 500X" and "que ha pasado, Adrian" -- it never
    asks who he is, because it already knows.

    The other three are for when that fails: a borrowed phone, a number
    not on the policy, a caller ringing about someone else's car.  Then
    the plate is the easiest to say aloud -- four digits and three
    letters, against eight for a DNI.
    """
    keys = {k: (_phone if k == "telefono" else _norm)(
                (query.get(k) or [""])[0])
            for k in ("poliza", "dni", "matricula", "telefono")}
    if not any(keys.values()):
        return 400, {"error": "indique_telefono_matricula_poliza_o_dni"}
    for row in POLIZAS:
        if any(value and (_phone(row[key]) if key == "telefono"
                          else _norm(row[key])) == value
               for key, value in keys.items()):
            return 200, row
    return 404, {"error": "poliza_no_encontrada",
                 "buscado": {k: v or None for k, v in keys.items()}}


def abrir_siniestro(query, body):
    """POST /v1/siniestros — mint an expediente for a known policy."""
    poliza = _norm(body.get("poliza", ""))
    known = next((r for r in POLIZAS if _norm(r["poliza"]) == poliza), None)
    if known is None:
        return 404, {"error": "poliza_no_encontrada", "poliza": body.get("poliza")}
    missing = [k for k in ("fecha", "lugar", "descripcion") if not body.get(k)]
    if missing:
        return 400, {"error": "faltan_datos", "campos": missing}
    return 200, {
        "expediente": f"EXP-2026-{random.randint(100000, 999999)}",
        "poliza": known["poliza"],
        "estado": "abierto",
        "perito_asignado": True,
        "proximo_paso": ("Un perito se pondrá en contacto en 48 horas "
                         "laborables para valorar los daños."),
    }


#: What an address service knows.  A real one covers the country; this
#: holds the towns the demo uses, because a mock that invented a
#: postcode for any input would be teaching the agent to trust made-up
#: data -- which is the failure this whole persona is written against.
CALLEJERO = [
    {"localidad": "Marchena", "provincia": "Sevilla", "cp": "41620"},
    {"localidad": "Sevilla", "provincia": "Sevilla", "cp": "41001"},
    {"localidad": "Madrid", "provincia": "Madrid", "cp": "28001"},
    {"localidad": "Getafe", "provincia": "Madrid", "cp": "28901"},
    {"localidad": "Alcalá de Henares", "provincia": "Madrid", "cp": "28801"},
]


def _fold(text: str) -> str:
    """Compare town names as they are SAID, not as they are spelled."""
    import unicodedata
    plain = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in plain if c.isalnum())


def normalizar_direccion(query, body):
    """GET /v1/direcciones?localidad=…&calle=…&numero=…

    Completes what the caller could not supply.  In the reference
    recording the caller answered "ni idea" to the postcode and the
    assistant simply had it -- resolving an address is the service's
    job, not the customer's memory.
    """
    localidad = _fold((query.get("localidad") or [""])[0])
    if not localidad:
        return 400, {"error": "indique_localidad"}
    for row in CALLEJERO:
        if _fold(row["localidad"]) == localidad:
            return 200, {
                "calle": (query.get("calle") or [""])[0] or None,
                "numero": (query.get("numero") or [""])[0] or None,
                "localidad": row["localidad"],
                "provincia": row["provincia"],
                "codigo_postal": row["cp"],
            }
    return 404, {"error": "localidad_no_encontrada",
                 "buscado": (query.get("localidad") or [""])[0]}


ROUTES = {
    ("GET", "/v1/polizas"): buscar_poliza,
    ("POST", "/v1/siniestros"): abrir_siniestro,
    ("GET", "/v1/direcciones"): normalizar_direccion,
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def log_message(self, fmt, *args):
        print(f"  [mock] {fmt % args}", flush=True)

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        handler = ROUTES.get((method, path))
        if handler is None:
            self._respond(404, {"error": "not_found", "path": path})
            return
        body = {}
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._respond(400, {"error": "cuerpo_no_es_json"})
                return
        status, payload = handler(parse_qs(parsed.query), body)
        self._respond(status, payload)

    def _respond(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(port: int = PORT) -> HTTPServer:
    """Start the server on a daemon thread and return it."""
    server = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    serve()
    print(f"  simulated Línea Directa systems on http://127.0.0.1:{PORT}")
    print("  demo policies (say one of these on the phone):")
    for row in POLIZAS:
        print(f"    {row['poliza']}  ·  DNI {row['dni']}  ·  {row['titular']}"
              f"  ·  {row['matricula']}  ·  {row['cobertura']}")
    print("  Ctrl-C to stop")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
