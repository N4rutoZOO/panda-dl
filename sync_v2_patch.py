"""Small V2 compatibility patches kept separate from the core sync engine."""


def install(module):
    original_parse = module._parse_import

    def parse_import_v2(raw_text: str, limit: int):
        text = raw_text or ""
        if "#EXTINF:" in text:
            tracks = []
            for raw in text.splitlines():
                line = raw.strip()
                if not line.startswith("#EXTINF:"):
                    continue
                meta = line.split(",", 1)[1].strip() if "," in line else ""
                if meta:
                    tracks.append(meta)
            if tracks:
                return original_parse("\n".join(tracks), limit)
        return original_parse(text, limit)

    module._parse_import = parse_import_v2
    return module
