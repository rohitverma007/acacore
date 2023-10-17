from datetime import datetime
from os import PathLike
from pathlib import Path
from re import compile as re_compile
from subprocess import CompletedProcess
from subprocess import run
from typing import Literal
from typing import Optional
from typing import Union

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator
from pydantic.networks import AnyUrl
from pydantic.networks import HttpUrl

from acacore.exceptions.files import IdentificationError

_byte_match_regexp_single = re_compile(r"^(.*; )?byte match at (\d+), *(\d+) *(\([^)]*\))?(;.*)?$")
_byte_match_regexp_multi = re_compile(r"^(.*; )?byte match at \[\[(\d+) +(\d+)]( \[\d+ +\d+])*] *(\([^)]*\))?(;.*)?$")
_extension_match = re_compile(r"^(.*; )?extension match ([^;]+)(;.*)?$")
TSignature = Literal["pronom", "loc", "tika", "freedesktop", "pronom-tika-loc", "deluxe", "archivematica"]


def _check_process(process: CompletedProcess) -> CompletedProcess:
    """
    Raises:
        IdentificationError: if the process ends with a return code other than 0
    """
    if process.returncode != 0:
        raise IdentificationError(
            process.stderr or process.stdout or f"Unknown siegfried error code {process.returncode}"
        )

    return process


class SiegfriedIdentifier(BaseModel):
    name: str
    details: str


class SiegfriedMatch(BaseModel):
    ns: str
    id: Optional[str]
    format: str
    version: Optional[str] = None
    mime: str
    match_class: Optional[str] = Field(None, alias="class")
    basis: str
    warning: str
    URI: Optional[AnyUrl] = None
    permalink: Optional[HttpUrl] = None

    def byte_match(self) -> Optional[int]:
        """
        Get the length of the byte match, if any, or None.

        Returns:
            The length of the byte match or None, if the match was not on the basis of bytes.
        """
        match = _byte_match_regexp_single.match(self.basis) or _byte_match_regexp_multi.match(self.basis)
        return (int(match.group(3)) - int(match.group(2))) if match else None

    def extension_match(self) -> Optional[str]:
        """
        Get the matched extension.

        Returns:
            The matched extension or None, if the match was not on the basis of the extension.
        """
        match = _extension_match.match(self.basis)
        return match.group(2) if match else None

    def extension_mismatch(self) -> bool:
        """
        Check whether the match has an extension mismatch warning.

        Returns:
            True if the match has an extension mismatch warning, False otherwise
        """
        return "extension mismatch" in self.warning

    def filename_mismatch(self) -> bool:
        """
        Check whether the match has a filename mismatch warning.

        Returns:
            True if the match has a filename mismatch warning, False otherwise
        """
        return "filename mismatch" in self.warning

    def sort_tuple(self) -> tuple[int, int, int, int, int]:
        """
        Get a tuple of integers useful for sorting matches. The fields used for the tuple are: byte match,
        extension match, format, version, and mime.

        Returns:
            A tuple of 5 integers.
        """
        return (
            self.byte_match() or 0,
            1 if self.extension_match() else 0,
            1 if self.format else 0,
            1 if self.version else 0,
            1 if self.mime else 0,
        )

    # noinspection PyNestedDecorators
    @field_validator("id")
    @classmethod
    def unknown_id(cls, _id: Optional[str]):
        _id = (_id or "").strip()
        return None if _id.lower() == "unknown" else _id or None


class SiegfriedFile(BaseModel):
    filename: str
    filesize: int
    modified: datetime
    errors: str
    matches: list[SiegfriedMatch]

    def best_match(self) -> Optional[SiegfriedMatch]:
        """
        Get the best match for the file.

        Returns:
            A SiegfriedMatch object or None if there are no known matches.
        """
        matches: list[SiegfriedMatch] = [m for m in self.matches if m.id]
        matches.sort(key=SiegfriedMatch.sort_tuple)
        return matches[-1] if matches else None

    def best_matches(self) -> list[SiegfriedMatch]:
        """
        Get the matches for the file sorted by how good they are; best are first.

        Returns:
            A list of SiegfriedMatch objects.
        """
        return sorted([m for m in self.matches if m.id], key=SiegfriedMatch.sort_tuple, reverse=True)


class SiegfriedResult(BaseModel):
    siegfried: str
    scandate: datetime
    signature: str
    created: datetime
    identifiers: list[SiegfriedIdentifier]
    files: list[SiegfriedFile]
    model_config = ConfigDict(extra="forbid")


class Siegfried:
    """
    A wrapper class to use the Siegfried program with Python and return the results with Pydantic models.

    See Also:
        https://github.com/richardlehane/siegfried
    """

    def __init__(self, binary: Union[str, PathLike] = "sf", signature: str = "default.sig"):
        """
        Args:
            binary: The path to the Siegfried binary, or the program name if it is included in the PATH variable.
            signature: The signature file to use with Siegfried.
        """
        self.binary: str = str(binary)
        self.signature: str = signature

    def run(self, *args: str) -> CompletedProcess:
        """
        Run the Siegfried command

        Args:
            *args: The arguments to be given to Siegfried (excluding the binary path/name).

        Returns:
            A subprocess.CompletedProcess object.

        Raises:
            IdentificationError: If Siegfried exits with a non-zero status code.
        """
        return _check_process(run([self.binary, *args], capture_output=True, encoding="utf-8"))

    def update(self, signature: TSignature, *, set_signature: bool = True):
        """
        Update or fetch signature files.

        Args:
            signature: The name of signatures provider; one of: "pronom", "loc", "tika", "freedesktop",
            "pronom-tika-loc", "deluxe", "archivematica".
            set_signature: Set to True to automatically change the signature to the newly updated one.

        Raises:
            IdentificationError: If Siegfried exits with a non-zero status code.
        """
        signature = signature.lower()

        self.run("-sig", f"{signature}.sig", "-update", signature)

        if set_signature:
            self.signature = f"{signature}.sig"

    def identify(self, path: Union[str, PathLike]) -> SiegfriedResult:
        """
        Identify a file.

        Args:
            path: The path to the file

        Returns:
            A SiegfriedResult object

        Raises:
            IdentificationError: If there is an error calling Siegfried or processing its results
        """
        process: CompletedProcess = self.run("-sig", self.signature, "-json", "-multi", "1024", str(path))
        try:
            return SiegfriedResult.model_validate_json(process.stdout)
        except ValueError as err:
            raise IdentificationError(err)

    def identify_many(self, paths: list[Path]) -> tuple[tuple[Path, SiegfriedFile]]:
        """
        Identify multiple files.

        Args:
            paths: The paths to the files

        Returns:
            A tuple of tuples joining the paths with their SiegfriedFile result

        Raises:
            IdentificationError: If there is an error calling Siegfried or processing its results
        """
        process: CompletedProcess = self.run("-sig", self.signature, "-json", "-multi", "1024", *map(str, paths))
        try:
            result = SiegfriedResult.model_validate_json(process.stdout)
            return tuple(zip(paths, result.files))
        except ValueError as err:
            raise IdentificationError(err)
