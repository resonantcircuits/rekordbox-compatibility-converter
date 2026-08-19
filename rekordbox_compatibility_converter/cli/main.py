"""Command-line interface for Rekordbox Format Checker & Converter."""

import os
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn
from rich.table import Table

from ..core.audio_converter import AudioConverter
from ..core.engine import ConversionEngine
from ..core.models import CompatibilityProfileType, TargetFormat
from ..core.profiles import PROFILES, get_profile
from ..core.usb_detector import USBDetector

console = Console()


def resolve_usb_path(path_arg: Optional[str]) -> Path:
    """Resolves provided path or auto-detects connected Rekordbox USB."""
    if path_arg:
        target = Path(path_arg).expanduser().resolve()
        if not target.exists():
            console.print(f"[red]Error:[/red] Path does not exist: {target}")
            raise click.Abort()
        return target

    detected = USBDetector.list_rekordbox_drives()
    if not detected:
        console.print("[yellow]No Rekordbox USB drives automatically detected.[/yellow]")
        console.print("Please specify the path to your USB drive, e.g.:")
        console.print("  [cyan]rbconvert scan /Volumes/YOUR_USB[/cyan]")
        raise click.Abort()

    if len(detected) == 1:
        drive_path, label = detected[0]
        console.print(f"[green]Auto-detected Rekordbox drive:[/green] [bold]{label}[/bold] ({drive_path})")
        return drive_path

    console.print("[cyan]Multiple Rekordbox drives detected:[/cyan]")
    for idx, (drive_path, label) in enumerate(detected, 1):
        console.print(f"  [{idx}] {label} ({drive_path})")

    choice = click.prompt("Select drive number", type=int, default=1)
    if 1 <= choice <= len(detected):
        return detected[choice - 1][0]
    raise click.Abort()


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx):
    """Rekordbox Format Checker & CDJ Compatibility Converter."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(scan)


@cli.command()
@click.argument("path", required=False, type=str)
@click.option(
    "--profile",
    "-p",
    type=click.Choice([p.value for p in CompatibilityProfileType], case_sensitive=False),
    default=CompatibilityProfileType.STANDARD.value,
    help="Target CDJ hardware profile.",
)
@click.option(
    "--format",
    "-f",
    "target_format",
    type=click.Choice(["aiff", "wav", "mp3"], case_sensitive=False),
    default="aiff",
    help="Target conversion format (default: aiff).",
)
def scan(path: Optional[str], profile: str, target_format: str):
    """Scans a Rekordbox USB drive and reports compatibility status."""
    usb_root = resolve_usb_path(path)
    hw_profile = get_profile(CompatibilityProfileType(profile))
    engine = ConversionEngine()

    console.print(
        Panel(
            f"[bold cyan]Scanning Rekordbox Drive:[/bold cyan] {usb_root}\n"
            f"[bold cyan]Target Profile:[/bold cyan] {hw_profile.name}\n"
            f"[bold cyan]Default Target Format:[/bold cyan] {target_format.upper()}",
            title="Rekordbox Compatibility Scan",
            expand=False,
        )
    )

    with console.status("[bold green]Reading export.pdb database...[/bold green]"):
        summary = engine.scan(
            usb_root=usb_root,
            profile=hw_profile,
            forced_target_format=TargetFormat(target_format.lower()),
        )

    if not summary.has_export_pdb:
        console.print("[red]Error: No PIONEER/rekordbox/export.pdb found on this drive.[/red]")
        return

    table = Table(title="Audio Format Breakdown", show_header=True, header_style="bold magenta")
    table.add_column("Format", style="cyan")
    table.add_column("Count", justify="right")
    table.add_column("Status", justify="center")

    for fmt, count in sorted(summary.format_counts.items(), key=lambda x: -x[1]):
        if fmt in hw_profile.allowed_formats:
            status = "[green]✓ Compatible[/green]"
        else:
            status = "[bold red]✗ Incompatible[/bold red]"
        table.add_row(f".{fmt.upper()}", str(count), status)

    console.print(table)
    console.print(f"\n[bold]Total Tracks:[/bold] {summary.total_tracks}")
    console.print(f"[bold green]Compatible Tracks:[/bold green] {summary.compatible_tracks}")

    if summary.incompatible_tracks == 0:
        console.print("\n[bold green]🎉 All tracks on this USB are 100% compatible with the selected profile![/bold green]\n")
        return

    console.print(f"[bold red]Incompatible Tracks:[/bold red] {summary.incompatible_tracks}")

    task_table = Table(title="Tracks Requiring Conversion (Sample)", show_header=True, header_style="bold yellow")
    task_table.add_column("ID", justify="right", style="dim")
    task_table.add_column("Title", style="bold")
    task_table.add_column("Current File")
    task_table.add_column("Current Spec", style="dim")
    task_table.add_column("Target File", style="green")

    for t in summary.tasks[:10]:
        cur_spec = f"{t.track.sample_rate}Hz {t.track.sample_depth}bit"
        task_table.add_row(str(t.track.id), t.track.title or "(No Title)", t.track.filename, cur_spec, t.target_filename)

    console.print(task_table)
    if len(summary.tasks) > 10:
        console.print(f"[dim]... and {len(summary.tasks) - 10} more tracks.[/dim]\n")

    console.print(
        f"[yellow]To convert these tracks, run:[/yellow] [bold cyan]rbconvert convert {usb_root} --profile {profile}[/bold cyan]\n"
    )


@cli.command()
@click.argument("path", required=False, type=str)
@click.option(
    "--profile",
    "-p",
    type=click.Choice([p.value for p in CompatibilityProfileType], case_sensitive=False),
    default=CompatibilityProfileType.STANDARD.value,
    help="Target CDJ hardware profile.",
)
@click.option(
    "--format",
    "-f",
    "target_format",
    type=click.Choice(["aiff", "wav", "mp3"], case_sensitive=False),
    default="aiff",
    help="Target conversion format (default: aiff).",
)
@click.option(
    "--threads",
    "-t",
    type=int,
    default=min(8, os.cpu_count() or 4),
    help="Number of parallel audio conversion worker threads.",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Bypass confirmation prompts and start conversion immediately.",
)
@click.option(
    "--keep-originals",
    is_flag=True,
    default=False,
    help="Do not delete original audio files after successful conversion (requires extra disk space).",
)
@click.option(
    "--no-backup",
    is_flag=True,
    default=False,
    help="Skip creating .bak backups of export.pdb and ANLZ files.",
)
@click.option(
    "--clean-dotfiles/--no-clean-dotfiles",
    default=True,
    help="Clean hidden macOS AppleDouble (._*) and .DS_Store ghost files to prevent CDJ read errors.",
)
@click.option(
    "--eject",
    is_flag=True,
    default=False,
    help="Safely unmount/eject the USB drive after conversion completes.",
)
def convert(
    path: Optional[str],
    profile: str,
    target_format: str,
    threads: int,
    yes: bool,
    keep_originals: bool,
    no_backup: bool,
    clean_dotfiles: bool,
    eject: bool,
):
    """Converts incompatible tracks on the USB and updates databases & waveforms."""
    usb_root = resolve_usb_path(path)
    hw_profile = get_profile(CompatibilityProfileType(profile))
    engine = ConversionEngine()

    converter = AudioConverter()
    tools_ok, msg = converter.check_tools()
    if not tools_ok:
        console.print(f"[bold red]Error:[/bold red] {msg}")
        console.print("Please install ffmpeg (e.g. `brew install ffmpeg` on macOS or download from ffmpeg.org).")
        raise click.Abort()

    summary = engine.scan(
        usb_root=usb_root,
        profile=hw_profile,
        forced_target_format=TargetFormat(target_format.lower()),
    )

    if not summary.has_export_pdb:
        console.print("[red]Error: No PIONEER/rekordbox/export.pdb found on this drive.[/red]")
        return

    if summary.incompatible_tracks == 0:
        console.print("\n[bold green]All tracks are already compatible! No conversion needed.[/bold green]\n")
        return

    console.print(
        Panel(
            f"[bold]Target Drive:[/bold] {usb_root}\n"
            f"[bold]Target Profile:[/bold] {hw_profile.name}\n"
            f"[bold]Tracks to Convert:[/bold] [bold red]{len(summary.tasks)}[/bold red]\n"
            f"[bold]Target Audio Format:[/bold] [bold green]{target_format.upper()}[/bold green]\n"
            f"[bold]Parallel Workers:[/bold] {threads} threads\n"
            f"[bold]Delete Originals After Conversion:[/bold] {'Yes (Space-saving Option A)' if not keep_originals else 'No (Keep originals)'}\n"
            f"[bold]Clean macOS Ghost Files (._*):[/bold] {'Yes' if clean_dotfiles else 'No'}\n"
            f"[bold]Create Database Backups:[/bold] {'Yes (.bak)' if not no_backup else 'No'}",
            title="Conversion Confirmation",
            border_style="yellow",
        )
    )

    if not yes:
        confirm = click.confirm("Do you want to proceed with conversion?", default=True)
        if not confirm:
            console.print("[yellow]Conversion canceled by user.[/yellow]")
            return

    console.print(f"\n[bold green]Starting parallel conversion ({threads} threads) & database synchronization...[/bold green]\n")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        bar = progress.add_task("[cyan]Converting tracks in parallel...", total=len(summary.tasks))

        def on_progress(task, current, total):
            short_name = task.track.filename
            if len(short_name) > 30:
                short_name = short_name[:27] + "..."
            progress.update(bar, advance=1, description=f"[cyan]Finished ({current}/{total}): [bold]{short_name}[/bold]")

        result = engine.execute(
            summary=summary,
            delete_original=not keep_originals,
            backup=not no_backup,
            threads=threads,
            clean_dotfiles=clean_dotfiles,
            progress_callback=on_progress,
        )

    if result.get("success"):
        cleaned_msg = f"\n• Cleaned {result.get('cleaned_dotfiles', 0)} macOS ghost (._*) files." if clean_dotfiles else ""
        console.print(
            Panel(
                f"[bold green]✓ Successfully converted {result['completed']} tracks in parallel![/bold green]\n"
                f"• Database [cyan]export.pdb[/cyan] successfully patched and synced.\n"
                f"• Analysis beatgrids and waveforms ([cyan]ANLZ[/cyan]) paths updated.{cleaned_msg}\n"
                f"• USB is now fully ready for the CDJ booth!",
                title="Conversion Complete",
                border_style="green",
            )
        )
    else:
        console.print(
            Panel(
                f"[bold yellow]Completed with warnings:[/bold yellow]\n"
                f"• Succeeded: {result.get('completed', 0)}\n"
                f"• Failed: {result.get('failed', 0)}",
                title="Conversion Finished with Errors",
                border_style="red",
            )
        )

    if eject:
        success, emsg = engine.eject_drive(usb_root)
        if success:
            console.print(f"[bold green]✓ {emsg}[/bold green]")
        else:
            console.print(f"[yellow]Eject notice:[/yellow] {emsg}")


@cli.command()
@click.argument("path", required=False, type=str)
def restore(path: Optional[str]):
    """Restores export.pdb and ANLZ analysis files from their .bak backups."""
    usb_root = resolve_usb_path(path)
    engine = ConversionEngine()

    confirm = click.confirm(f"Are you sure you want to restore {usb_root} database from .bak backup?", default=False)
    if not confirm:
        console.print("[yellow]Restore canceled.[/yellow]")
        return

    success, msg = engine.restore_backup(usb_root)
    if success:
        console.print(f"[bold green]✓ {msg}[/bold green]")
    else:
        console.print(f"[bold red]Error:[/bold red] {msg}")


@cli.command()
@click.argument("path", required=False, type=str)
def verify(path: Optional[str]):
    """Validates the integrity of all database entries, audio files, and ANLZ tags."""
    from ..core.validator import ExportValidator

    usb_root = resolve_usb_path(path)
    console.print(Panel(f"[bold cyan]Validating USB Export:[/bold cyan] {usb_root}", title="Rekordbox Export Validator"))

    validator = ExportValidator()
    with console.status("[bold green]Running integrity checks on database & analysis files...[/bold green]"):
        report = validator.validate(usb_root)

    console.print(f"\n[bold]Total Tracks Inspected:[/bold] {report.total_tracks_checked}")
    console.print(f"[bold green]Passed Checks:[/bold green] {report.passed_tracks}")
    if report.failed_tracks > 0:
        console.print(f"[bold red]Issues Detected:[/bold red] {report.failed_tracks}")

    if report.issues:
        issue_table = Table(title="Validation Issues Found", show_header=True, header_style="bold red")
        issue_table.add_column("ID", justify="right", style="dim")
        issue_table.add_column("Title", style="bold")
        issue_table.add_column("Severity", justify="center")
        issue_table.add_column("Details")

        for issue in report.issues[:20]:
            sev_style = "[bold red]ERROR[/bold red]" if issue.severity == "ERROR" else "[yellow]WARN[/yellow]"
            issue_table.add_row(str(issue.track_id), issue.track_title, sev_style, issue.message)

        console.print(issue_table)
        if len(report.issues) > 20:
            console.print(f"[dim]... and {len(report.issues) - 20} more issues.[/dim]\n")
    else:
        console.print("\n[bold green]✓ All tracks, database entries, and ANLZ waveform references are 100% valid![/bold green]\n")


@cli.command()
def drives():
    """Lists all detected Rekordbox export USB drives."""
    detected = USBDetector.list_rekordbox_drives()
    if not detected:
        console.print("[yellow]No Rekordbox USB drives found.[/yellow]")
        return

    table = Table(title="Detected Rekordbox Drives", show_header=True, header_style="bold cyan")
    table.add_column("Label / Volume", style="bold")
    table.add_column("Mount Path")

    for path, label in detected:
        table.add_row(label, str(path))

    console.print(table)


@cli.command()
def profiles():
    """Displays information on available CDJ compatibility profiles."""
    table = Table(title="Pioneer CDJ Compatibility Profiles", show_header=True, header_style="bold cyan")
    table.add_column("Profile ID", style="bold yellow")
    table.add_column("Name", style="bold")
    table.add_column("Target Hardware & Description")
    table.add_column("Default Target")

    for key, p in PROFILES.items():
        table.add_row(key.value, p.name, p.description, p.default_target_format.value.upper())

    console.print(table)


def main():
    cli()


if __name__ == "__main__":
    main()
