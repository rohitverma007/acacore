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
from pydantic import model_validator
from pydantic.networks import AnyUrl
from pydantic.networks import HttpUrl

from acacore.exceptions.files import IdentificationError

_byte_match_regexp_single = re_compile(r"^byte match at (\d+), *(\d+)( *\([^)]*\))?$")
_byte_match_regexp_multi = re_compile(r"^byte match at \[\[(\d+) +(\d+)]( \[\d+ +\d+])*]( \([^)]*\))?$")
_extension_match = re_compile(r"^extension match (.+)$")
TSignature = Literal["pronom", "loc", "tika", "freedesktop", "pronom-tika-loc", "deluxe", "archivematica"]


def _check_process(process: CompletedProcess) -> CompletedProcess:
    """
    Check process and raise exception if it failed.

    Raises:
        IdentificationError: if the process ends with a return code other than 0.
    """
    if process.returncode != 0:
        raise IdentificationError(process.stderr or process.stdout or f"Unknown error code {process.returncode}")

    return process


class SiegfriedIdentifier(BaseModel):
    """
    A class representing an identifiers used by the Siegfried identification tool.

    Attributes:
        name (str): The name of the Siegfried identifier.
        details (str): Additional details or description of the identifier.
    """

    name: str
    details: str


class SiegfriedMatch(BaseModel):
    """
    A class representing a match generated by the Siegfried identification tool.

    Attributes:
        ns (str): The namespace of the match.
        id (str, optional): The identifier of the match.
        format (str): The format of the match.
        version (str, optional): The version of the match.
        mime (str): The MIME type of the match.
        match_class (str, optional): The class of the match.
        basis (list[str]): The basis of the match.
        warning (list[str]): The warning messages of the match.
        URI (AnyUrl, optional): The URI of the match.
        permalink (HttpUrl, optional): The permalink of the match.
    """

    ns: str
    id: Optional[str]  # noqa: A003
    format: str  # noqa: A003
    version: Optional[str] = None
    mime: str
    match_class: Optional[str] = Field(None, alias="class")
    basis: list[str]
    warning: list[str]
    URI: Optional[AnyUrl] = None
    permalink: Optional[HttpUrl] = None

    def byte_match(self) -> Optional[int]:
        """
        Get the length of the byte match, if any, or None.

        Returns:
            The length of the byte match or None, if the match was not on the basis of bytes.
        """
        for basis in self.basis:
            match = _byte_match_regexp_single.match(basis) or _byte_match_regexp_multi.match(basis)
            if match:
                return (int(match.group(2)) - int(match.group(1))) if match else None
        return None

    def extension_match(self) -> Optional[str]:
        """
        Get the matched extension.

        Returns:
            The matched extension or None, if the match was not on the basis of the extension.
        """
        for basis in self.basis:
            match = _extension_match.match(basis)
            if match:
                return match.group(1) if match else None
        return None

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
        Get a tuple of integers useful for sorting matches.

        The fields used for the tuple are: byte match, extension match, format, version, and mime.

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
    @model_validator(mode="before")
    @classmethod
    def unknown_id(cls, data: dict | object):
        if isinstance(data, dict):
            return {
                **data,
                "id": None if data["id"].lower().strip() == "unknown" else data["id"].strip() or None,
                "basis": filter(bool, map(str.strip, data["basis"].strip().split(";"))),
                "warning": filter(bool, map(str.strip, data["warning"].strip().split(";"))),
            }
        return data


class SiegfriedFile(BaseModel):
    """
    The SiegfriedFile class represents a file that has been analyzed by Siegfried.

    It contains information about the file's name, size, modification date, matching results, and any
    errors encountered during analysis.

    Attributes:
        filename (str): The name of the file.
        filesize (int): The size of the file in bytes.
        modified (datetime): The modification date of the file.
        errors (str): Any errors encountered during analysis.
        matches (list[SiegfriedMatch]): The list of matches found for the file.
    """

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
    """
    Represents the result of a Siegfried signature scan.

    Attributes:
        siegfried (str): The version of Siegfried used for the scan.
        scandate (datetime): The date and time when the scan was performed.
        signature (str): The digital signature used for the scan.
        created (datetime): The date and time when the signature file was created.
        identifiers (List[SiegfriedIdentifier]): A list of identifiers used for file identification.
        files (List[SiegfriedFile]): A list of files that were scanned.
    """

    siegfried: str
    scandate: datetime
    signature: str
    created: datetime
    identifiers: list[SiegfriedIdentifier]
    files: list[SiegfriedFile]
    model_config = ConfigDict(extra="forbid")


class Siegfried:
    """
    A class for interacting with the Siegfried file identification tool.

    Attributes:
        binary (str): The path to the Siegfried binary or the program name if it is included in the PATH variable.
        signature (str): The signature file to use with Siegfried.

    See Also:
        https://github.com/richardlehane/siegfried
    """

    def __init__(self, binary: Union[str, PathLike] = "sf", signature: str = "default.sig") -> None:
        """
        Initializes a new instance of the Siegfried class.

        Args:
            binary: The path or name of the Siegfried binary. Defaults to "sf".
            signature: The name of the signature file to use. Defaults to "default.sig".
        """
        self.binary: str = str(binary)
        self.signature: str = signature

    def run(self, *args: str) -> CompletedProcess:
        """
        Run the Siegfried command.

        Args:
            *args: The arguments to be given to Siegfried (excluding the binary path/name).

        Returns:
            A subprocess.CompletedProcess object.

        Raises:
            IdentificationError: If Siegfried exits with a non-zero status code.
        """
        return _check_process(run([self.binary, *args], capture_output=True, encoding="utf-8"))  # noqa: PLW1510

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
