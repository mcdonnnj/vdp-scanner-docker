"""Check current federal DotGov domains for a Vulnerability Disclosure Policy (VDP).

Usage:
    vdp_scanner.py [options] local FILE
    vdp_scanner.py [options] github

Commands:
    local   Use the provided CSV file as the source of domains to scan.
    github  Use the CSV of Federal domains from the cisagov/dotgov-data
            repository as the source of domains to scan.

Arguments:
    FILE  The local CSV file to use.

Options:
    -h, --help                   Show this help message.
    -v, --version                Show script version.
    -d, --debug                  Enable debugging output.
    -a, --agency-csv=AGENCY_CSV  Filename to use for agency results.
    -t, --domain-csv=DOMAIN_CSV  Filename to use for domain (TLD) results.
    -p, --path-to-chromium=PATH  Specify the Chromium binary to use.
                                 [default: /usr/bin/chromium]
"""

# Standard Python Libraries
from collections import defaultdict
import csv
from datetime import datetime
import logging
from os.path import join as path_join
from typing import Any, Dict, List, NamedTuple, Optional, Tuple
from urllib.parse import urlparse, urlunparse

# Third-Party Libraries
import docopt
import requests
import urllib3

# cisagov Libraries
from hash_http_content import UrlHasher, UrlResult

GITHUB_CSV_URL = (
    "https://raw.githubusercontent.com/cisagov/dotgov-data/main/current-federal.csv"
)


class DomainResult(NamedTuple):
    """Structured format for a domain check result."""

    domain: str
    agency: str
    organization: str
    security_contact: str
    visited_url: str
    is_redirect: bool
    vdp_present: bool
    vdp_hash: str


class VdpScanner:
    """Class to handle scanning and outputting the results of any scans."""

    # Value that represents a missing security contact in a GSA formatted domain
    # list CSV.
    MISSING_SECURITY_CONTACT = "(blank)"

    # Header for the agency level results CSV.
    agency_csv_header = [
        "Agency",
        "Total Domains",
        "Domains with Security Contact Listed",
        "Domains with Organization Listed",
        "Domains with Matching Organization and Agency",
        "Domains with Published VDP",
    ]

    # Header for the domain level results CSV.
    domain_csv_header = [
        "Domain",
        "Agency",
        "Organization",
        "Security Contact Email",
        "Visited URL",
        "Was it Redirected",
        "VDP is Published",
        "VDP Hash",
    ]

    def __init__(self, hasher: UrlHasher):
        """Initialize variables and perform setup."""
        self._hasher = hasher
        file_date = datetime.utcnow().strftime("%Y-%m-%d")
        self.agency_csv = f"agency_results_{file_date}.csv"
        self.domain_csv = f"domain_results_{file_date}.csv"
        self.output_directory = "host_mount"

        self.agency_results: defaultdict = defaultdict(
            lambda: {k: 0 for k in self.agency_csv_header[1:]}
        )

        self.domain_results: List[Dict[str, Any]] = []

    @staticmethod
    def _log_vdp_failure(domain: str, err: Exception) -> None:
        """Log failure information during check_for_vdp() execution."""
        logging.warning("Unable to retrieve hash for '%s'", domain)
        logging.debug("Caught %s", type(err).__name__)
        logging.debug(err)

    def check_for_vdp(self, domain: str) -> Tuple[str, bool, bool, str]:
        """Check for a VDP at the given domain and return the relavent information."""
        url = urlparse(f"https://{domain}/vulnerability-disclosure-policy")
        result: Optional[UrlResult] = None

        # Try with HTTPS first
        try:
            result = self._hasher.hash_url(urlunparse(url))
        # If there is a TLS issue, try running it without verifying
        except requests.exceptions.SSLError:
            logging.warning(
                "Falling back to HTTPS without TLS verification for '%s'", domain
            )
            try:
                # Fallback to unverified TLS
                result = self._hasher.hash_url(urlunparse(url), verify=False)
            # If this also fails, fallback to HTTP
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                logging.warning("Falling back to HTTP for '%s'", domain)
                # Try connecting to the HTTP endpoint instead
                try:
                    result = self._hasher.hash_url(
                        urlunparse(url._replace(scheme="http"))
                    )
                # If we're unable to successfully retrieve the URL for some reason
                except Exception as err:
                    self._log_vdp_failure(domain, err)
            # The except of last resort
            except Exception as err:
                self._log_vdp_failure(domain, err)
        # Fallback to HTTP in case there is no HTTPS for the given domain
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            logging.warning("Falling back to HTTP for '%s'", domain)
            # Try connecting to the HTTP endpoint instead
            try:
                result = self._hasher.hash_url(urlunparse(url._replace(scheme="http")))
            # If we're unable to successfully retrieve the URL for some reason
            except Exception as err:
                self._log_vdp_failure(domain, err)
        # The except of last resort
        except Exception as err:
            self._log_vdp_failure(domain, err)

        if not result:
            return ("", False, False, "")

        if result.status == 200:
            return (result.visited_url, result.is_redirect, True, result.hash)

        return (result.visited_url, result.is_redirect, False, "")

    def process_domain(self, domain_info: Dict[str, Any]) -> None:
        """Process a domain entry from the DotGov CSV."""
        # These are direct copies from current-federal.csv
        vdp_result = self.check_for_vdp(domain_info["Domain Name"])

        self.add_domain_result(
            DomainResult(
                domain_info["Domain Name"],
                domain_info["Agency"],
                domain_info["Organization"],
                domain_info["Security Contact Email"],
                *vdp_result,
            )
        )

    def add_domain_result(self, result: DomainResult) -> None:
        """Process the provided results for a domain."""
        # Create a dict with the values of domain_csv_header as keys and the
        # contents of result as values. This leverages the fact that the
        # DomainResult NamedTuple is positionally aligned with the contents of
        # the domain_csv_header list.
        self.domain_results.append(dict(zip(self.domain_csv_header, result)))

        self.agency_results[result.agency]["Total Domains"] += 1

        if (
            result.security_contact
            and result.security_contact != self.MISSING_SECURITY_CONTACT
        ):
            self.agency_results[result.agency][
                "Domains with Security Contact Listed"
            ] += 1

        if result.organization:
            self.agency_results[result.agency]["Domains with Organization Listed"] += 1

        if result.agency == result.organization:
            self.agency_results[result.agency][
                "Domains with Matching Organization and Agency"
            ] += 1

        if result.vdp_present:
            self.agency_results[result.agency]["Domains with Published VDP"] += 1

    def output_agency_csv(self) -> None:
        """Output the agency results to a CSV."""
        file = path_join(self.output_directory, self.agency_csv)
        with open(file, "w") as csv_out:
            agency_output = csv.DictWriter(
                csv_out, fieldnames=VdpScanner.agency_csv_header
            )
            agency_output.writeheader()
            for agency, info in self.agency_results.items():
                output_dict = {"Agency": agency, **info}
                agency_output.writerow(output_dict)

    def output_domain_csv(self) -> None:
        """Output the agency results to a CSV."""
        file = path_join(self.output_directory, self.domain_csv)
        with open(file, "w") as csv_out:
            domain_output = csv.DictWriter(
                csv_out, fieldnames=VdpScanner.domain_csv_header
            )
            domain_output.writeheader()
            for result in self.domain_results:
                domain_output.writerow(result)

    def output_all_csvs(self) -> None:
        """Output all CSVs."""
        self.output_agency_csv()
        self.output_domain_csv()


def get_version(version_file) -> str:
    """Extract a version number from the given file path."""
    with open(version_file) as vfile:
        for line in vfile.read().splitlines():
            if line.startswith("__version__"):
                delim = '"' if '"' in line else "'"
                return line.split(delim)[1]

    raise RuntimeError("Unable to find version string.")


def get_local_csv(file: str) -> List[Dict[str, str]]:
    """Load domains from a local CSV file."""
    with open(file) as csv_file:
        csv_lines = [line.rstrip() for line in csv_file.readlines()]

    return list(csv.DictReader(csv_lines))


def get_remote_csv() -> List[Dict[str, str]]:
    """Load domains from the CSV at the given URL."""
    resp = requests.get(GITHUB_CSV_URL)
    if resp.status_code != 200:
        return []
    csv_lines = [str(line, resp.encoding) for line in resp.iter_lines()]

    return list(csv.DictReader(csv_lines))


def main():
    """Scan hosts with the hash-http-content package and output results."""
    __version__: str = get_version("version.txt")
    args: Dict[str, Any] = docopt.docopt(__doc__, version=__version__)

    log_level = logging.DEBUG if args["--debug"] else logging.INFO
    logging.basicConfig(
        format="%(asctime)-15s %(levelname)s %(message)s", level=log_level
    )

    # If we make a call to UrlHasher.hash_url() with verify=False, it will output
    # a warning. Since this is a fallback mechanism, we can squelch these warnings.
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    browser_opts = {
        "args": [
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-zygote",
        ],
        "executablePath": args["--path-to-chromium"],
    }
    http_hasher = UrlHasher("sha256", browser_options=browser_opts)

    scanner: VdpScanner = VdpScanner(http_hasher)
    if args["--agency-csv"]:
        scanner.agency_csv = args["--agency-csv"]
    if args["--domain-csv"]:
        scanner.domain_csv = args["--domain-csv"]

    domains_to_scan: List[Dict[str, str]]

    if args["local"]:
        domains_to_scan = get_local_csv(path_join("host_mount", args["FILE"]))

    if args["github"]:
        domains_to_scan = get_remote_csv()

    total_domains = len(domains_to_scan)
    for i, domain_info in enumerate(
        sorted(domains_to_scan, key=lambda d: d["Domain Name"]), start=1
    ):
        logging.info(
            "Processing '%s' (%d/%d)...", domain_info["Domain Name"], i, total_domains
        )
        scanner.process_domain(domain_info)

    scanner.output_all_csvs()


if __name__ == "__main__":
    main()
