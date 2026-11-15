"""Who is asking: the details that decide which HR rules apply to them. See docs/DECISIONS.md, D56.

They come from the HR system, never from the model. The employee can correct them on the chat screen, which only changes
which sections search prefers and how the answer is worded.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EmploymentType = Literal["permanent", "contract", "intern"]
# A place name of up to three words, such as "Navi Mumbai", as the location goes into the answer prompt. A longer
# phrase, or punctuation, could be written as an instruction.
PLACE_PATTERN = r"^[A-Za-z][A-Za-z'-]*( [A-Za-z][A-Za-z'-]*){0,2}$"
PLACE_CHARS = 30


class Persona(BaseModel):
    """What's known about the person asking. A missing detail is unknown, not "everyone"."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    band: int | None = Field(default=None, ge=1, le=20)
    location: str | None = Field(default=None, pattern=PLACE_PATTERN, max_length=PLACE_CHARS)
    employment_type: EmploymentType | None = None
    on_probation: bool | None = None

    def is_known(self) -> bool:
        return any(value is not None for value in self.model_dump().values())

    def corrected(self, correction: "Persona | None") -> "Persona":
        """The employee's own corrections win over the HR record, detail by detail."""
        if correction is None:
            return self
        return self.model_copy(update=correction.model_dump(exclude_none=True))

    def describe(self) -> str:
        """In words, for the answer prompt and the chat screen. Empty if nothing is known."""
        parts = []
        if self.band is not None:
            parts.append(f"Band {self.band}")
        if self.location:
            parts.append(f"based in {self.location}")
        if self.employment_type == "intern":
            parts.append("an intern")
        elif self.employment_type:
            parts.append(f"a {self.employment_type} employee")
        if self.on_probation is True:
            parts.append("on probation")
        elif self.on_probation is False:
            parts.append("confirmed, not on probation")
        return ", ".join(parts)
