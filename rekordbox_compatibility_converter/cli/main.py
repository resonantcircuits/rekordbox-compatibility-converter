"""Command-line interface for Rekordbox Format Checker & Converter."""

from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn
from rich.table import Table

from ..core.audio_converter import AudioConverter
from ..core.dlp_manager import ONELIBRARY_REBUILD_REQUIRED_MESSAGE
from ..core.engine import DEFAULT_CONVERSION_THREADS, ConversionEngine
from ..core.models import CompatibilityProfileType, TargetFormat
from ..core.profiles import PROFILES, get_profile
from ..core.usb_detector import USBDetector

console = Console()


def resolve_usb_path(path_arg: Optional[str]) -> Path:
    """Resolves provided path or auto-detects connected Rekordbox USB."""
    if path_arg:
        target = Path(path_arg).expanduser().resolve()
        if not target.exists():
            console.print(f"[red]Error:[/red] Path does not exist: {escape(str(target))}")
            raise click.Abort()
        if not target.is_dir():
            raise click.ClickException(f"Path is not a directory: {target}")
        return target

    detected = USBDetector.list_rekordbox_drives()
    if not detected:
        console.print("[yellow]No Rekordbox USB drives automatically detected.[/yellow]")
        console.print("Please specify the path to your USB drive, e.g.:")
        console.print("  [cyan]rbconvert scan /Volumes/YOUR_USB[/cyan]")
        raise click.Abort()

    if len(detected) == 1:
        drive_path, label = detected[0]
        console.print(
            f"[green]Auto-detected Rekordbox drive:[/green] "
            f"[bold]{escape(label)}[/bold] ({escape(str(drive_path))})"
        )
        return drive_path

    console.print("[cyan]Multiple Rekordbox drives detected:[/cyan]")
    for idx, (drive_path, label) in enumerate(detected, 1):
        console.print(f"  [{idx}] {escape(label)} ({escape(str(drive_path))})")

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
@click.option(
    "--enforce-16-bit",
    is_flag=True,
    default=False,
    help=(
        "Convert every lossless track above 16-bit, including otherwise-compatible WAV/AIFF, "
        "and make all lossless conversion output 16-bit."
    ),
)
@click.option(
    "--experimental-onelibrary-bridge",
    is_flag=True,
    help="Scan Device Library even when OneLibrary is present; OneLibrary is not modified.",
)
def scan(
    path: Optional[str],
    profile: str,
    target_format: str,
    enforce_16_bit: bool,
    experimental_onelibrary_bridge: bool,
):
    """Scans a Rekordbox USB drive and reports compatibility status."""
    usb_root = resolve_usb_path(path)
    hw_profile = get_profile(CompatibilityProfileType(profile))
    engine = ConversionEngine()

    console.print(
        Panel(
            f"[bold cyan]Scanning Rekordbox Drive:[/bold cyan] {escape(str(usb_root))}\n"
            f"[bold cyan]Target Profile:[/bold cyan] {escape(hw_profile.name)}\n"
            f"[bold cyan]Default Target Format:[/bold cyan] {target_format.upper()}\n"
            f"[bold cyan]Lossless Bit Depth:[/bold cyan] "
            f"{'Enforce 16-bit across USB' if enforce_16_bit else 'Profile default'}",
            title="Rekordbox Compatibility Scan",
            expand=False,
        )
    )

    with console.status("[bold green]Reading export.pdb database...[/bold green]"):
        summary = engine.scan(
            usb_root=usb_root,
            profile=hw_profile,
            forced_target_format=TargetFormat(target_format.lower()),
            enforce_pcm_16_bit=enforce_16_bit,
            allow_onelibrary_bridge=experimental_onelibrary_bridge,
        )

    if not summary.has_export_pdb:
        if summary.has_dlp:
            raise click.ClickException(summary.unsupported_reason)
        if summary.unsupported_reason:
            raise click.ClickException(summary.unsupported_reason)
        raise click.ClickException("No PIONEER/rekordbox/export.pdb found on this drive.")
    if summary.has_dlp and not summary.onelibrary_bridge_mode:
        raise click.ClickException(summary.unsupported_reason or "OneLibrary is unsupported.")

    if summary.onelibrary_bridge_mode:
        console.print(
            Panel(
                "[bold yellow]Experimental OneLibrary bridge scan.[/bold yellow]\n"
                "Only Device Library will be planned for conversion. OneLibrary remains unchanged "
                "until Rekordbox's Convert from Device Library command is run.",
                title="OneLibrary Follow-up Required",
                border_style="yellow",
            )
        )

    table = Table(title="Audio Format Breakdown", show_header=True, header_style="bold magenta")
    table.add_column("Format", style="cyan")
    table.add_column("Count", justify="right")
    table.add_column("Status", justify="center")

    for fmt, count in sorted(summary.format_counts.items(), key=lambda x: -x[1]):
        if fmt in hw_profile.allowed_formats:
            status = "[green]Format recognized[/green]"
        else:
            status = "[bold red]Format unsupported[/bold red]"
        table.add_row(f".{fmt.upper()}", str(count), status)

    console.print(table)
    console.print(f"\n[bold]Total Tracks:[/bold] {summary.total_tracks}")
    console.print(f"[bold green]Compatible Tracks:[/bold green] {summary.compatible_tracks}")

    if summary.analysis_repairs:
        console.print(
            f"[bold yellow]Waveform Paths to Repair:[/bold yellow] "
            f"{len(summary.analysis_repairs)}"
        )
        repair_table = Table(
            title="Tracks Requiring Waveform Path Repair",
            show_header=True,
            header_style="bold yellow",
        )
        repair_table.add_column("ID", justify="right", style="dim")
        repair_table.add_column("Title", style="bold")
        repair_table.add_column("Stored Audio Path", style="dim")
        repair_table.add_column("Current Audio Path", style="green")
        for repair in summary.analysis_repairs[:10]:
            repair_table.add_row(
                str(repair.track.id),
                repair.track.title or repair.track.filename,
                repair.old_audio_path,
                repair.new_audio_path,
            )
        console.print(repair_table)

    if summary.bitrate_repairs:
        console.print(
            f"[bold yellow]Device Library Metadata to Repair:[/bold yellow] "
            f"{len(summary.bitrate_repairs)}"
        )

    if (
        summary.incompatible_tracks == 0
        and not summary.analysis_repairs
        and not summary.bitrate_repairs
    ):
        console.print("\n[bold green]All tracks are compatible with the selected profile.[/bold green]\n")
        return

    if summary.incompatible_tracks == 0:
        if summary.bitrate_repairs:
            repair_message = "stored Device Library metadata needs repair"
        else:
            repair_message = "stored waveform paths need repair"
        console.print(
            f"\n[green]All audio formats are compatible, but {repair_message}.[/green]\n"
        )
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
        task_table.add_row(
            str(t.track.id),
            escape(t.track.title or "(No Title)"),
            escape(t.track.filename),
            cur_spec,
            escape(t.target_filename),
        )

    console.print(task_table)
    if len(summary.tasks) > 10:
        console.print(f"[dim]... and {len(summary.tasks) - 10} more tracks.[/dim]\n")

    bridge_flag = " --experimental-onelibrary-bridge" if summary.onelibrary_bridge_mode else ""
    depth_flag = " --enforce-16-bit" if enforce_16_bit else ""
    console.print(
        f"[yellow]To convert these tracks, run:[/yellow] "
        f"[bold cyan]rbconvert convert {escape(str(usb_root))} --profile {profile}{depth_flag}{bridge_flag}[/bold cyan]\n"
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
    "--enforce-16-bit",
    is_flag=True,
    default=False,
    help=(
        "Convert every lossless track above 16-bit, including otherwise-compatible WAV/AIFF, "
        "and make all lossless conversion output 16-bit."
    ),
)
@click.option(
    "--threads",
    "-t",
    type=click.IntRange(1, 32),
    default=DEFAULT_CONVERSION_THREADS,
    show_default=True,
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
    "--original-backup-dir",
    type=click.Path(path_type=Path, file_okay=False, resolve_path=True),
    help=(
        "Archive and verify original audio in this local folder before removing it from the USB. "
        "Required for the space-saving OneLibrary bridge workflow."
    ),
)
@click.option(
    "--replace-existing-targets",
    is_flag=True,
    default=False,
    help=(
        "Resolve existing conversion targets: audio-verify and reuse referenced targets, and "
        "archive and replace unreferenced targets. Requires --original-backup-dir."
    ),
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
@click.option(
    "--experimental-onelibrary-bridge",
    is_flag=True,
    help=(
        "Archive originals locally, patch Device Library, then rebuild OneLibrary from Device "
        "Library in Rekordbox."
    ),
)
def convert(
    path: Optional[str],
    profile: str,
    target_format: str,
    enforce_16_bit: bool,
    threads: int,
    yes: bool,
    keep_originals: bool,
    original_backup_dir: Optional[Path],
    replace_existing_targets: bool,
    no_backup: bool,
    clean_dotfiles: bool,
    eject: bool,
    experimental_onelibrary_bridge: bool,
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
        enforce_pcm_16_bit=enforce_16_bit,
        allow_onelibrary_bridge=experimental_onelibrary_bridge,
    )

    if not summary.has_export_pdb:
        if summary.has_dlp:
            raise click.ClickException(summary.unsupported_reason)
        raise click.ClickException("No PIONEER/rekordbox/export.pdb found on this drive.")

    if summary.has_dlp and not summary.onelibrary_bridge_mode:
        raise click.ClickException(summary.unsupported_reason or "OneLibrary is unsupported.")

    if summary.onelibrary_bridge_mode:
        if no_backup:
            raise click.ClickException(
                "--no-backup cannot be used with --experimental-onelibrary-bridge."
            )
        if not original_backup_dir:
            raise click.ClickException(
                "--original-backup-dir is required with --experimental-onelibrary-bridge."
            )
        if keep_originals:
            raise click.ClickException(
                "--keep-originals cannot be combined with --original-backup-dir."
            )
        keep_originals = False
        no_backup = False
    elif original_backup_dir and keep_originals:
        raise click.ClickException(
            "--keep-originals cannot be combined with --original-backup-dir."
        )

    existing_targets = [
        task
        for task in summary.tasks
        if task.target_abs_path.is_file()
        and engine._path_key(task.target_abs_path)
        != engine._path_key(task.source_abs_path)
    ]
    if replace_existing_targets and not original_backup_dir:
        raise click.ClickException(
            "--replace-existing-targets requires --original-backup-dir."
        )
    if existing_targets and not original_backup_dir:
        raise click.ClickException(
            f"Found {len(existing_targets)} existing conversion target(s). Provide "
            "--original-backup-dir so they can be archived before replacement."
        )
    if existing_targets and not replace_existing_targets:
        if yes:
            raise click.ClickException(
                f"Found {len(existing_targets)} existing conversion target(s). Explicitly pass "
                "--replace-existing-targets to archive and regenerate them."
            )
        replace_existing_targets = click.confirm(
            f"Safely resolve {len(existing_targets)} existing target file(s)?",
            default=False,
        )
        if not replace_existing_targets:
            console.print("[yellow]Conversion canceled by user.[/yellow]")
            return

    if (
        summary.incompatible_tracks == 0
        and not summary.analysis_repairs
        and not summary.bitrate_repairs
    ):
        console.print("\n[bold green]All tracks are already compatible! No conversion needed.[/bold green]\n")
        return

    console.print(
        Panel(
            f"[bold]Target Drive:[/bold] {escape(str(usb_root))}\n"
            f"[bold]Target Profile:[/bold] {escape(hw_profile.name)}\n"
            f"[bold]Tracks to Convert:[/bold] [bold red]{len(summary.tasks)}[/bold red]\n"
            f"[bold]Waveform Paths to Repair:[/bold] [bold yellow]{len(summary.analysis_repairs)}[/bold yellow]\n"
            f"[bold]Device Library Metadata Repairs:[/bold] [bold yellow]{len(summary.bitrate_repairs)}[/bold yellow]\n"
            f"[bold]Target Audio Format:[/bold] [bold green]{target_format.upper()}[/bold green]\n"
            f"[bold]Lossless Bit Depth:[/bold] {'Enforce 16-bit across USB' if enforce_16_bit else 'Profile default'}\n"
            f"[bold]Parallel Workers:[/bold] {threads} threads\n"
            f"[bold]Local Original Archive:[/bold] {escape(str(original_backup_dir)) if original_backup_dir else 'Disabled'}\n"
            f"[bold]Original Handling:[/bold] "
            f"{'Verify locally, then remove before conversion' if original_backup_dir else 'Delete after durable commit' if not keep_originals else 'Keep originals on USB'}\n"
            f"[bold]Clean macOS Ghost Files (._*):[/bold] {'Yes' if clean_dotfiles else 'No'}\n"
            f"[bold]Create Database Backups:[/bold] {'Yes (.bak)' if not no_backup else 'No'}",
            title=(
                "Experimental OneLibrary Bridge"
                if summary.onelibrary_bridge_mode
                else "Conversion Confirmation"
            ),
            border_style="yellow",
        )
    )

    if summary.onelibrary_bridge_mode:
        console.print(
            Panel(
                "This will patch only Device Library. Original audio will first be copied and "
                "verified in the selected local archive, then removed from the USB. Afterward, Rekordbox must overwrite OneLibrary "
                "using OneLibrary > Convert from Device Library. OneLibrary-only playlists or "
                "histories may be lost. Test only on a fully copied USB.",
                border_style="yellow",
            )
        )

    if not yes:
        confirm = click.confirm(
            "Do you want to proceed with conversion?",
            default=not summary.onelibrary_bridge_mode,
        )
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
        bar = progress.add_task(
            "[cyan]Processing planned tracks...",
            total=(
                len(summary.tasks)
                + len(summary.analysis_repairs)
                + len(summary.bitrate_repairs)
            ),
        )

        def on_progress(task, current, total):
            short_name = escape(task.track.filename)
            if len(short_name) > 30:
                short_name = short_name[:27] + "..."
            progress.update(bar, advance=1, description=f"[cyan]Finished ({current}/{total}): [bold]{short_name}[/bold]")

        def on_phase(phase, current, total, detail):
            if phase == "waveform_repair":
                progress.update(
                    bar,
                    total=max(1, total),
                    completed=current,
                    description=f"[cyan]Repairing waveform paths: [bold]{escape(detail)}[/bold]",
                )
            elif phase == "metadata_repair":
                progress.update(
                    bar,
                    total=max(1, total),
                    completed=current,
                    description=f"[cyan]Repairing Device Library metadata: [bold]{escape(detail)}[/bold]",
                )

        result = engine.execute(
            summary=summary,
            delete_original=not keep_originals,
            backup=not no_backup,
            threads=threads,
            clean_dotfiles=clean_dotfiles,
            progress_callback=on_progress,
            phase_callback=on_phase,
            allow_onelibrary_bridge=summary.onelibrary_bridge_mode,
            local_original_backup_dir=original_backup_dir,
            replace_existing_targets=replace_existing_targets,
        )

    if result.get("success"):
        adopted = result.get("adopted_existing_targets", 0)
        converted = result["completed"] - adopted
        adopted_msg = (
            f"\n• Relinked {adopted} stale database row(s) to strictly verified existing files."
            if adopted
            else ""
        )
        repaired = result.get("analysis_paths_repaired", 0)
        repaired_msg = (
            f"\n• Repaired stale waveform paths for {repaired} track(s)."
            if repaired
            else ""
        )
        metadata_repaired = result.get("bitrate_metadata_repaired", 0)
        metadata_repaired_msg = (
            f"\n• Corrected Device Library bitrate units for {metadata_repaired} track(s)."
            if metadata_repaired
            else ""
        )
        cleaned_msg = f"\n• Cleaned {result.get('cleaned_dotfiles', 0)} macOS ghost files." if clean_dotfiles else ""
        original_msg = (
            f"• Available originals and metadata are preserved in {escape(str(result.get('local_backup_session')))}."
            if result.get("local_backup_session")
            else "• Original removal was attempted only after each durable database commit."
            if not keep_originals
            else "• Original audio files were retained."
        )
        result_warnings = result.get("warnings") or []
        warning_msg = f"\n• Warning: {escape(result_warnings[0])}" if result_warnings else ""
        console.print(
            Panel(
                f"[bold green]Successfully converted {converted} tracks.[/bold green]"
                f"{adopted_msg}{repaired_msg}{metadata_repaired_msg}\n"
                f"• Database [cyan]export.pdb[/cyan] successfully patched and synced.\n"
                f"• Updated {result.get('anlz_updated', 0)} analysis path files ([cyan]ANLZ[/cyan]).{cleaned_msg}\n"
                f"{original_msg}{warning_msg}",
                title="Conversion Complete",
                border_style="green",
            )
        )
        if result.get("onelibrary_sync_required"):
            console.print(
                Panel(
                    escape(ONELIBRARY_REBUILD_REQUIRED_MESSAGE),
                    title="Required Rekordbox Step",
                    border_style="yellow",
                )
            )
    else:
        error_detail = escape(str(result.get("error") or "One or more tracks failed."))
        preflight = result.get("preflight_errors") or []
        task_errors = [task.error for task in summary.tasks if task.error]
        errors = preflight or task_errors
        first_error = f"\n• First error: {escape(errors[0])}" if errors else ""
        console.print(
            Panel(
                f"[bold red]Conversion did not complete successfully.[/bold red]\n"
                f"• Succeeded: {result.get('completed', 0)}\n"
                f"• Failed: {result.get('failed', 0)}\n"
                f"• {error_detail}{first_error}",
                title="Conversion Finished with Errors",
                border_style="red",
            )
        )

    if eject and result.get("success"):
        success, emsg = engine.eject_drive(usb_root)
        if success:
            console.print(f"[bold green]{escape(emsg)}[/bold green]")
        else:
            console.print(f"[yellow]Eject notice:[/yellow] {escape(emsg)}")

    if not result.get("success"):
        raise click.exceptions.Exit(1)


@cli.command("restore-local-backup")
@click.argument(
    "session",
    type=click.Path(path_type=Path, exists=True, file_okay=False, resolve_path=True),
)
@click.option(
    "--usb",
    "usb_path",
    type=click.Path(path_type=Path, exists=True, file_okay=False, resolve_path=True),
    help="Current USB mount path. Defaults to the path recorded in the backup manifest.",
)
@click.option("--yes", "confirmed", is_flag=True, help="Confirm restoration without prompting.")
def restore_local_backup(session: Path, usb_path: Optional[Path], confirmed: bool):
    """Restores audio and all Rekordbox metadata from a verified local session."""
    if not confirmed and not click.confirm(
        "Restore original audio and Rekordbox databases from this local archive? "
        "Verified converted replacements will be removed.",
        default=False,
    ):
        console.print("[yellow]Restore canceled.[/yellow]")
        return
    success, message = ConversionEngine().restore_local_backup(session, usb_path)
    if not success:
        raise click.ClickException(message)
    console.print(f"[bold green]{escape(message)}[/bold green]")


@cli.command()
@click.argument("path", required=False, type=str)
def restore(path: Optional[str]):
    """Restores export.pdb and ANLZ analysis files from their .bak backups."""
    usb_root = resolve_usb_path(path)
    engine = ConversionEngine()

    dlp_paths = (
        usb_root / "PIONEER" / "DeviceLibraryPlus" / "exportLibrary.db",
        usb_root / "PIONEER" / "rekordbox" / "exportLibrary.db",
    )
    restore_warning = f"Restore {usb_root} Device Library and ANLZ files from .bak backup?"
    if any(path.is_file() for path in dlp_paths):
        restore_warning += (
            "\n\nOneLibrary will not be restored. If it has already been rebuilt, you must "
            "run OneLibrary > Convert from Device Library in Rekordbox again after this restore."
        )
    confirm = click.confirm(restore_warning, default=False)
    if not confirm:
        console.print("[yellow]Restore canceled.[/yellow]")
        return

    success, msg = engine.restore_backup(usb_root)
    if success:
        console.print(f"[bold green]{msg}[/bold green]")
    else:
        raise click.ClickException(msg)


@cli.command("cleanup-originals")
@click.argument("path", required=False, type=str)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Confirm permanent removal after all safety checks pass.",
)
def cleanup_originals(path: Optional[str], yes: bool):
    """Removes originals retained until the OneLibrary rebuild was verified."""
    usb_root = resolve_usb_path(path)
    engine = ConversionEngine()
    with console.status("[bold green]Verifying retained originals and replacements...[/bold green]"):
        plan = engine.plan_retained_original_cleanup(usb_root)

    if plan.errors:
        details = "\n".join(f"- {escape(error)}" for error in plan.errors)
        raise click.ClickException(f"Cleanup safety checks failed; no files were removed.\n{details}")

    reclaim_gib = plan.total_bytes / (1024 ** 3)
    reclaim_mib = plan.total_bytes / (1024 ** 2)
    reclaim_text = f"{reclaim_gib:.2f} GiB" if reclaim_gib >= 1 else f"{reclaim_mib:.1f} MiB"
    console.print(
        Panel(
            f"[bold]Retained originals:[/bold] {len(plan.candidates)}\n"
            f"[bold]Space to reclaim:[/bold] {reclaim_text}\n\n"
            "Every converted replacement, Device Library entry, and ANLZ path passed validation.",
            title="Retained Original Cleanup",
            border_style="yellow",
        )
    )
    if plan.warnings:
        console.print(f"[yellow]Important:[/yellow] {escape(plan.warnings[0])}")

    confirmation = (
        "Permanently delete these originals? Continue only after Rekordbox OneLibrary > "
        "Convert from Device Library completed, you verified the converted tracks in "
        "OneLibrary, and you have another copy of the original audio."
    )
    if not yes and not click.confirm(confirmation, default=False):
        console.print("[yellow]Cleanup canceled; no files were removed.[/yellow]")
        return

    result = engine.cleanup_retained_originals(plan)
    if not result.get("success"):
        details = "\n".join(
            f"- {escape(error)}" for error in result.get("errors", [])
        )
        raise click.ClickException(
            f"Cleanup did not complete. Removed {result.get('removed', 0)} originals.\n{details}"
        )

    console.print(
        f"[bold green]Removed {result['removed']} verified originals and reclaimed "
        f"approximately {reclaim_text}.[/bold green]"
    )


@cli.command()
@click.argument("path", required=False, type=str)
@click.option(
    "--profile",
    "-p",
    type=click.Choice([p.value for p in CompatibilityProfileType], case_sensitive=False),
    default=CompatibilityProfileType.STANDARD.value,
    help="Validate actual audio against this CDJ hardware profile.",
)
def verify(path: Optional[str], profile: str):
    """Validates the integrity of all database entries, audio files, and ANLZ tags."""
    from ..core.validator import ExportValidator

    usb_root = resolve_usb_path(path)
    console.print(
        Panel(
            f"[bold cyan]Validating USB Export:[/bold cyan] {escape(str(usb_root))}",
            title="Rekordbox Export Validator",
        )
    )

    validator = ExportValidator()
    with console.status("[bold green]Running integrity checks on database & analysis files...[/bold green]"):
        report = validator.validate(
            usb_root, profile=get_profile(CompatibilityProfileType(profile))
        )

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
            issue_table.add_row(
                str(issue.track_id),
                escape(issue.track_title),
                sev_style,
                escape(issue.message),
            )

        console.print(issue_table)
        if len(report.issues) > 20:
            console.print(f"[dim]... and {len(report.issues) - 20} more issues.[/dim]\n")
    else:
        console.print("\n[bold green]All checked audio, database entries, and ANLZ references are valid.[/bold green]\n")

    if report.issues:
        raise click.exceptions.Exit(1)


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
        table.add_row(escape(label), escape(str(path)))

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
