from __future__ import annotations

from typing import Any, Dict, Iterable, Optional


RT_PLUS_AID = 0x4BD7
RT_PLUS_CONTENT_FIELDS = {
    1: "title",
    2: "album",
    4: "artist",
    11: "genre",
}


RDS_PTY_LABELS = (
    "None",
    "News",
    "Current Affairs",
    "Information",
    "Sport",
    "Education",
    "Drama",
    "Culture",
    "Science",
    "Varied",
    "Pop Music",
    "Rock Music",
    "Easy Listening",
    "Light Classical",
    "Serious Classical",
    "Other Music",
    "Weather",
    "Finance",
    "Children's Programmes",
    "Social Affairs",
    "Religion",
    "Phone In",
    "Travel",
    "Leisure",
    "Jazz Music",
    "Country Music",
    "National Music",
    "Oldies Music",
    "Folk Music",
    "Documentary",
    "Alarm Test",
    "Alarm",
)


def _decode_rds_character(value: int) -> str:
    byte = int(value) & 0xFF
    if byte in {0x00, 0xFF}:
        return " "
    if 0x20 <= byte <= 0x7E:
        return chr(byte)
    if byte >= 0xA0:
        return bytes((byte,)).decode("latin-1")
    return " "


def _decode_rds_bytes(values: Iterable[int]) -> str:
    characters = []
    for value in values:
        byte = int(value) & 0xFF
        if byte == 0x0D:
            break
        characters.append(_decode_rds_character(byte))
    return " ".join("".join(characters).strip().split())


# Punctuation broadcasters actually use inside an 8 character PS field.
_PS_ALLOWED_PUNCTUATION = " &'\"-.,:!?/+#*%"


def is_valid_rds_station_name(value: object) -> bool:
    """Return whether a decoded Program Service name is implausible enough to drop.

    A PS field assembled out of RDS blocks that passed the chip's block error
    test but decoded to the wrong bytes still looks like text, so it reaches the
    UI and the scan cache. Real names are alphanumeric with the occasional
    punctuation mark; anything carrying characters such as ``^`` or ``¬`` is a
    decode miss rather than a station.

    This is a character-content check, so it cannot catch a corrupt assembly
    that happens to be all letters and digits (a live run produced "aruU"). It
    is a guard against obvious garbage, not proof that a name is real; repeated
    assembly in :class:`RdsDecoder` is what covers the rest.
    """
    text = str(value or "")
    if not any(character.isalnum() for character in text):
        return False
    return all(character.isalnum() or character in _PS_ALLOWED_PUNCTUATION for character in text)


class RdsDecoder:
    """Assemble the most useful IEC 62106/RBDS fields from raw RDS groups."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.program_identification: Optional[int] = None
        self.groups_decoded = 0
        self.groups_received = 0
        self.groups_rejected = 0
        self.block_error_counts = [0, 0, 0, 0]
        self.group_type_counts: Dict[str, int] = {}
        self._reset_program_state()

    def _reset_program_state(self) -> None:
        """Drop every field that belongs to the current programme identifier.

        Called from :meth:`reset` and again whenever block A names a new
        programme, so a half collected PS or RT segment from the previous
        station can never be concatenated with the new one.
        """
        self.program_type_code: Optional[int] = None
        self.traffic_program = False
        self.traffic_announcement = False
        self.program_service = ""
        self.radio_text = ""
        self.radio_text_raw = ""
        self.text_ab: Optional[int] = None
        self.rt_plus_group_type: Optional[int] = None
        self.rt_plus_item_toggle: Optional[int] = None
        self.rt_plus_item_running = False
        self.rt_plus_values: Dict[str, str] = {}
        self._rt_plus_tags: Dict[int, tuple[int, int]] = {}
        self._ps_chars = [" "] * 8
        self._ps_segments = 0
        self._pending_ps_name: Optional[str] = None
        self._pending_ps_count = 0
        self._pending_pty: Optional[int] = None
        self._pending_pty_count = 0
        self._rt_chars = [" "] * 64
        self._rt_segments = 0
        self._rt_width = 64
        self._radio_text_values: list[int] = []

    @staticmethod
    def _block_characters(block: int) -> tuple[int, int]:
        return ((int(block) >> 8) & 0xFF, int(block) & 0xFF)

    def consume(self, group: Dict[str, Any]) -> bool:
        block_a = int(group.get("block_a") or 0) & 0xFFFF
        block_b = int(group.get("block_b") or 0) & 0xFFFF
        block_c = int(group.get("block_c") or 0) & 0xFFFF
        block_d = int(group.get("block_d") or 0) & 0xFFFF
        ble = tuple(int(value) for value in group.get("ble", (0, 0, 0, 0)))
        self.groups_received += 1
        if len(ble) == 4:
            for index, level in enumerate(ble):
                if level:
                    self.block_error_counts[index] += 1
        if len(ble) != 4 or ble[1] >= 3:
            self.groups_rejected += 1
            return False

        changed = False
        if ble[0] < 3 and block_a != self.program_identification:
            if self.program_identification is not None:
                self._reset_program_state()
            self.program_identification = block_a
            changed = True

        pty = (block_b >> 5) & 0x1F
        tp = bool(block_b & (1 << 10))
        # Block B is where a marginal signal shows up first: the live log had
        # one station flip between Pop Music, Alarm Test and Religion within
        # seconds. Only publish a programme type seen twice in a row.
        if pty != self._pending_pty:
            self._pending_pty = pty
            self._pending_pty_count = 1
        else:
            self._pending_pty_count += 1
        if self._pending_pty_count >= 2 and pty != self.program_type_code:
            self.program_type_code = pty
            changed = True
        if tp != self.traffic_program:
            self.traffic_program = tp
            changed = True

        group_type = (block_b >> 12) & 0x0F
        version_b = bool(block_b & (1 << 11))
        group_name = f"{group_type}{'B' if version_b else 'A'}"
        self.group_type_counts[group_name] = self.group_type_counts.get(group_name, 0) + 1
        if group_type == 0 and ble[3] < 3:
            changed = self._consume_program_service(block_b, block_d) or changed
        elif group_type == 2:
            changed = self._consume_radio_text(block_b, block_c, block_d, ble, version_b) or changed
        elif group_type == 3 and not version_b and ble[3] < 3 and block_d == RT_PLUS_AID:
            application_group = (block_b >> 1) & 0x0F
            if application_group != self.rt_plus_group_type:
                self.rt_plus_group_type = application_group
                self._rt_plus_tags = {}
                self.rt_plus_values = {}
                changed = True
        elif (
            self.rt_plus_group_type is not None
            and group_type == self.rt_plus_group_type
            and not version_b
            and ble[2] < 3
            and ble[3] < 3
        ):
            changed = self._consume_rt_plus(block_b, block_c, block_d) or changed

        self.groups_decoded += 1
        return changed

    def _consume_program_service(self, block_b: int, block_d: int) -> bool:
        segment = block_b & 0x03
        offset = segment * 2
        first, second = self._block_characters(block_d)
        self._ps_chars[offset : offset + 2] = [chr(first), chr(second)]
        self._ps_segments |= 1 << segment

        traffic_announcement = bool(block_b & (1 << 4))
        changed = traffic_announcement != self.traffic_announcement
        self.traffic_announcement = traffic_announcement
        if self._ps_segments == 0x0F:
            value = _decode_rds_bytes(ord(char) for char in self._ps_chars)
            self._ps_segments = 0
            # One corrupted pass still assembles into something that looks like
            # text ("BBC R1¬," and "aruU" both appeared live), so a name has to
            # survive two complete, identical assemblies before it is trusted.
            # A name we would refuse to show is simply skipped so it cannot
            # push the real name back to zero.
            if is_valid_rds_station_name(value):
                if value == self._pending_ps_name:
                    self._pending_ps_count += 1
                else:
                    self._pending_ps_name = value
                    self._pending_ps_count = 1
                if self._pending_ps_count >= 2 and value != self.program_service:
                    self.program_service = value
                    changed = True
        return changed

    def _consume_radio_text(
        self,
        block_b: int,
        block_c: int,
        block_d: int,
        ble: tuple[int, int, int, int],
        version_b: bool,
    ) -> bool:
        changed = False
        text_ab = 1 if block_b & (1 << 4) else 0
        if text_ab != self.text_ab:
            changed = bool(self.radio_text or self.rt_plus_values)
            self.text_ab = text_ab
            self._rt_chars = [" "] * 64
            self._rt_segments = 0
            self.radio_text = ""
            self.radio_text_raw = ""
            self._radio_text_values = []
            self._rt_plus_tags = {}
            self.rt_plus_values = {}

        segment = block_b & 0x0F
        if version_b:
            if ble[3] >= 3:
                return False
            values = self._block_characters(block_d)
            width = 32
            offset = segment * 2
        else:
            if ble[2] >= 3 or ble[3] >= 3:
                return False
            values = self._block_characters(block_c) + self._block_characters(block_d)
            width = 64
            offset = segment * 4

        self._rt_width = width
        for index, value in enumerate(values):
            self._rt_chars[offset + index] = chr(value)
        self._rt_segments |= 1 << segment

        raw_values = [ord(char) for char in self._rt_chars[:width]]
        terminator_index = raw_values.index(0x0D) if 0x0D in raw_values else None
        required_segment_count = (
            (terminator_index // len(values)) + 1
            if terminator_index is not None
            else width // len(values)
        )
        required_segments = (1 << required_segment_count) - 1
        if (self._rt_segments & required_segments) != required_segments:
            return changed

        message_values = raw_values[:terminator_index] if terminator_index is not None else raw_values
        raw_text = "".join(_decode_rds_character(value) for value in message_values).rstrip()
        value = _decode_rds_bytes(message_values)
        self._rt_segments = 0
        if value == self.radio_text and raw_text == self.radio_text_raw:
            return changed
        self._radio_text_values = message_values
        self.radio_text_raw = raw_text
        self.radio_text = value
        self._apply_rt_plus_tags()
        return True

    def _consume_rt_plus(self, block_b: int, block_c: int, block_d: int) -> bool:
        item_toggle = 1 if block_b & (1 << 4) else 0
        item_running = bool(block_b & (1 << 3))
        changed = item_running != self.rt_plus_item_running
        if self.rt_plus_item_toggle is not None and item_toggle != self.rt_plus_item_toggle:
            self._rt_plus_tags = {}
            self.rt_plus_values = {}
            changed = True
        if item_toggle != self.rt_plus_item_toggle:
            changed = True
        self.rt_plus_item_toggle = item_toggle
        self.rt_plus_item_running = item_running

        tags = (
            (
                ((block_b & 0x07) << 3) | ((block_c >> 13) & 0x07),
                (block_c >> 7) & 0x3F,
                (block_c >> 1) & 0x3F,
            ),
            (
                ((block_c & 0x01) << 5) | ((block_d >> 11) & 0x1F),
                (block_d >> 5) & 0x3F,
                block_d & 0x1F,
            ),
        )
        for content_type, start, length in tags:
            if content_type == 0:
                continue
            marker = (start, length)
            if self._rt_plus_tags.get(content_type) != marker:
                self._rt_plus_tags[content_type] = marker
                changed = True
        return self._apply_rt_plus_tags() or changed

    def _apply_rt_plus_tags(self) -> bool:
        if not self._radio_text_values:
            return False
        values = dict(self.rt_plus_values)
        for content_type, (start, length) in self._rt_plus_tags.items():
            field = RT_PLUS_CONTENT_FIELDS.get(content_type)
            if field is None:
                continue
            end = start + length + 1
            if start >= len(self._radio_text_values) or end > len(self._radio_text_values):
                continue
            value = _decode_rds_bytes(self._radio_text_values[start:end])
            if value:
                values[field] = value
        if values == self.rt_plus_values:
            return False
        self.rt_plus_values = values
        return True

    def snapshot(self) -> Dict[str, Any]:
        pty = self.program_type_code
        return {
            "program_identification": self.program_identification,
            "program_identification_hex": (
                f"{self.program_identification:04X}" if self.program_identification is not None else None
            ),
            "program_type_code": pty,
            "program_type": RDS_PTY_LABELS[pty] if pty is not None else None,
            "traffic_program": self.traffic_program,
            "traffic_announcement": self.traffic_announcement,
            "station_name": self.program_service or None,
            "text": self.radio_text,
            "text_raw": self.radio_text_raw,
            "text_ab": self.text_ab,
            "groups_decoded": self.groups_decoded,
            "groups_received": self.groups_received,
            "groups_rejected": self.groups_rejected,
            "block_error_counts": list(self.block_error_counts),
            "group_type_counts": dict(self.group_type_counts),
            "rt_plus": self.rt_plus_group_type is not None,
            "rt_plus_group_type": (
                f"{self.rt_plus_group_type}A" if self.rt_plus_group_type is not None else None
            ),
            "rt_plus_item_toggle": self.rt_plus_item_toggle,
            "rt_plus_item_running": self.rt_plus_item_running,
            "title": self.rt_plus_values.get("title"),
            "artist": self.rt_plus_values.get("artist"),
            "album": self.rt_plus_values.get("album"),
            "genre": self.rt_plus_values.get("genre"),
        }
