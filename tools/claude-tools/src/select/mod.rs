pub mod state;

use std::fs::File;
use std::io::{self, BufRead, BufReader};
use std::path::PathBuf;
use std::time::Duration;

use crossterm::event::{self, Event, KeyCode, KeyEventKind};
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use crossterm::ExecutableCommand;
use ratatui::backend::CrosstermBackend;
use ratatui::prelude::*;
use ratatui::widgets::{Block, Borders, Paragraph};
use ratatui::Terminal;

use crate::context::tui::theme;
use state::{AppState, ListItem};
#[derive(Debug, PartialEq)]
struct SelectArgs {
    title: String,
    items_path: Option<PathBuf>,
}

/// Parse `select` arguments, rejecting anything unrecognised.
///
/// Silently skipping unknown flags is what let this break in the first place:
/// `helpers.sh` shipped `--items <file>` before the binary understood it, the
/// old parser ignored it, and the menu vanished with exit 0. A hard error makes
/// the next caller/binary skew loud — the caller treats a non-zero exit as
/// "keep the defaults", which is the safe direction.
fn parse_args(args: &[String]) -> Result<SelectArgs, String> {
    let mut parsed = SelectArgs {
        title: "Select components".to_string(),
        items_path: None,
    };
    let mut i = 1; // args[0] is the subcommand name
    while i < args.len() {
        let flag = args[i].as_str();
        match flag {
            "--title" | "--items" => {
                let value = args
                    .get(i + 1)
                    .ok_or_else(|| format!("{flag} requires a value"))?;
                if flag == "--title" {
                    parsed.title = value.clone();
                } else {
                    parsed.items_path = Some(PathBuf::from(value));
                }
                i += 2;
            }
            other => {
                return Err(format!(
                    "unrecognised option {other:?} (supported: --title <text>, --items <file>)"
                ))
            }
        }
    }
    Ok(parsed)
}

fn read_items<R: BufRead>(reader: R) -> Result<Vec<ListItem>, io::Error> {
    let mut items = Vec::new();
    let mut last_group: Option<String> = None;

    for line in reader.lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }

        let parts: Vec<&str> = line.splitn(4, '|').collect();
        if parts.len() < 4 {
            continue;
        }
        let group = parts[0].trim().to_string();
        let name = parts[1].trim().to_string();
        let description = parts[2].trim().to_string();
        let checked = parts[3].trim() == "true";

        if last_group.as_deref() != Some(&group) {
            items.push(ListItem::GroupHeader {
                name: group.clone(),
            });
            last_group = Some(group);
        }

        items.push(ListItem::Component {
            name,
            description,
            selected: checked,
        });
    }

    Ok(items)
}

pub fn run(args: Vec<String>) -> Result<(), Box<dyn std::error::Error>> {
    let args = parse_args(&args).map_err(|e| format!("claude-tools select: {e}"))?;
    let items = match &args.items_path {
        // --items keeps stdin on the terminal. Piping items in makes fd 0 a pipe,
        // which pushes crossterm onto a fragile /dev/tty fallback for keyboard
        // input ("Failed to initialize input reader" on some terminals).
        Some(path) => read_items(BufReader::new(File::open(path)?))?,
        None => read_items(io::stdin().lock())?,
    };

    // Loud, not silent: returning Ok(()) here printed nothing on stdout, and the
    // caller read "no output" as "deselect everything".
    if items.is_empty() {
        return Err("claude-tools select: no items to choose from".into());
    }

    let mut state = AppState::new(items);

    // Render TUI to stderr so stdout stays clean for selected-names output.
    // This is critical: deploy.sh captures our stdout in result=$(...) and
    // uses each line as a variable name — any escape codes there cause errors.
    enable_raw_mode()?;
    io::stderr().execute(EnterAlternateScreen)?;

    let result = run_loop(&mut state, &args.title);

    let _ = disable_raw_mode();
    let _ = io::stderr().execute(LeaveAlternateScreen);

    result?;

    if state.cancelled {
        std::process::exit(1);
    }

    // Print selected names to stdout (clean, no escape codes)
    for name in state.selected_names() {
        println!("{}", name);
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{parse_args, read_items, SelectArgs};
    use crate::select::state::ListItem;
    use std::io::Cursor;
    use std::path::PathBuf;

    #[test]
    fn parses_items_path_and_title() {
        let args = vec![
            "claude-tools-select".to_string(),
            "--title".to_string(),
            "Select deploy components".to_string(),
            "--items".to_string(),
            "/tmp/components".to_string(),
        ];

        assert_eq!(
            parse_args(&args).expect("known flags must parse"),
            SelectArgs {
                title: "Select deploy components".to_string(),
                items_path: Some(PathBuf::from("/tmp/components")),
            }
        );
    }

    #[test]
    fn rejects_unrecognised_and_incomplete_flags() {
        // A silently-skipped unknown flag is the exact bug this guards: helpers.sh
        // sent --items to a binary that ignored it, and the menu vanished.
        // argv[0] is the synthetic program name main.rs prepends, never a flag.
        for argv in [
            vec![
                "claude-tools-select".to_string(),
                "--itmes".to_string(),
                "/x".to_string(),
            ],
            vec!["claude-tools-select".to_string(), "--items".to_string()],
            vec!["claude-tools-select".to_string(), "--title".to_string()],
            vec!["claude-tools-select".to_string(), "stray".to_string()],
        ] {
            assert!(
                parse_args(&argv).is_err(),
                "expected {argv:?} to be rejected"
            );
        }
    }

    /// Caller/callee parity: every long option `helpers.sh` actually passes to
    /// `claude-tools select` must be one this parser accepts. Unit tests over
    /// parse_args alone all passed while the menu was broken, because the defect
    /// lived in the shell↔binary contract, not in either side on its own.
    #[test]
    fn helpers_sh_passes_only_flags_this_parser_accepts() {
        let helpers = std::fs::read_to_string(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../scripts/shared/helpers.sh"
        ))
        .expect("helpers.sh is the caller under test");

        let invocation = helpers
            .lines()
            .find(|l| l.contains("claude-tools select"))
            .expect("helpers.sh must invoke `claude-tools select`");

        let flags: Vec<&str> = invocation
            .split_whitespace()
            .filter(|t| t.starts_with("--"))
            .collect();
        assert!(
            !flags.is_empty(),
            "found no flags in {invocation:?} — the extractor is vacuous"
        );

        for flag in flags {
            let argv = vec![
                "claude-tools-select".to_string(),
                flag.to_string(),
                "placeholder".to_string(),
            ];
            assert!(
                parse_args(&argv).is_ok(),
                "helpers.sh passes {flag}, which this parser rejects"
            );
        }
    }

    /// parse_args starts at index 1 because main.rs prepends a synthetic program
    /// name. If main.rs ever forwarded the real argv instead, index 1 would land
    /// on the "select" subcommand token and the strict parser would reject every
    /// invocation — a break the tests above cannot see, since they supply argv[0]
    /// themselves.
    #[test]
    fn main_prepends_a_synthetic_argv0_before_the_select_flags() {
        let main_rs = std::fs::read_to_string(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/src/main.rs"
        ))
        .expect("main.rs is the dispatcher under test");

        let dispatch = main_rs
            .split_once("\"select\" =>")
            .expect("main.rs must dispatch the select subcommand")
            .1;
        let dispatch = &dispatch[..dispatch.find("select::run").expect("dispatch calls select::run")];

        assert!(
            dispatch.contains("\"claude-tools-select\".to_string()"),
            "select dispatch no longer seeds a synthetic argv[0]: {dispatch}"
        );
        assert!(
            dispatch.contains("args[2..]"),
            "select dispatch no longer forwards args[2..] (the flags only): {dispatch}"
        );
    }

    #[test]
    fn parses_grouped_items_from_any_reader() {
        let input = Cursor::new(
            "Base|shell|Shell config|true\n\
             Base|tmux|Tmux config|false\n\
             AI|claude|Claude config|true\n",
        );

        let items = read_items(input).expect("items should parse");

        assert_eq!(items.len(), 5);
        assert!(matches!(
            &items[0],
            ListItem::GroupHeader { name } if name == "Base"
        ));
        assert!(matches!(
            &items[1],
            ListItem::Component { name, selected: true, .. } if name == "shell"
        ));
        assert!(matches!(
            &items[2],
            ListItem::Component { name, selected: false, .. } if name == "tmux"
        ));
        assert!(matches!(
            &items[3],
            ListItem::GroupHeader { name } if name == "AI"
        ));
    }
}

fn run_loop(state: &mut AppState, title: &str) -> Result<(), Box<dyn std::error::Error>> {
    // Backend on stderr; raw mode + alternate screen are managed by the caller.
    let backend = CrosstermBackend::new(std::io::stderr());
    let mut terminal = Terminal::new(backend)?;

    // Slow idle tick: forces a full repaint to self-heal mosh smearing while idle.
    // 1.5 s is infrequent enough that it won't visibly strobe even over a slow link.
    const IDLE_TICK: Duration = Duration::from_millis(1500);

    loop {
        terminal.draw(|f| render(f, state, title))?;

        if event::poll(IDLE_TICK)? {
            match event::read()? {
                Event::Key(key) => {
                    if key.kind != KeyEventKind::Press {
                        continue;
                    }
                    match key.code {
                        KeyCode::Char('q') | KeyCode::Esc => {
                            state.cancelled = true;
                            break;
                        }
                        KeyCode::Enter => {
                            state.confirmed = true;
                            break;
                        }
                        KeyCode::Char(' ') => state.toggle(),
                        KeyCode::Down | KeyCode::Char('j') => state.move_down(),
                        KeyCode::Up | KeyCode::Char('k') => state.move_up(),
                        // Ctrl-L: force full repaint to heal mosh/terminal desync
                        KeyCode::Char('l')
                            if key
                                .modifiers
                                .contains(crossterm::event::KeyModifiers::CONTROL) =>
                        {
                            terminal.clear()?;
                        }
                        _ => {}
                    }
                }
                // Resize: clear and redraw immediately to heal desynced cells
                Event::Resize(..) => {
                    terminal.clear()?;
                }
                _ => {}
            }
        }
        // Idle tick: redraw (the loop continues, terminal.draw fires at top)
    }

    Ok(())
}

fn render(f: &mut ratatui::Frame, state: &AppState, title: &str) {
    let area = f.area();

    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(3),
            Constraint::Min(1),
            Constraint::Length(2),
        ])
        .split(area);

    // Header
    let header = Paragraph::new(vec![
        Line::from(vec![Span::styled(format!(" {} ", title), theme::header())]),
        Line::from(vec![
            Span::styled(" j/k ", theme::hint()),
            Span::raw("navigate  "),
            Span::styled("space ", theme::hint()),
            Span::raw("toggle  "),
            Span::styled("enter ", theme::hint()),
            Span::raw("confirm  "),
            Span::styled("q ", theme::hint()),
            Span::raw("cancel  "),
            Span::styled("ctrl-l ", theme::hint()),
            Span::raw("repaint"),
        ]),
    ])
    .block(Block::default().borders(Borders::BOTTOM));
    f.render_widget(header, chunks[0]);

    // List area
    let list_area = chunks[1];
    let mut lines: Vec<Line> = Vec::new();

    let visible_height = list_area.height as usize;
    let scroll_offset = if state.cursor > visible_height / 2 {
        state.cursor.saturating_sub(visible_height / 2)
    } else {
        0
    };

    for (i, item) in state
        .items
        .iter()
        .enumerate()
        .skip(scroll_offset)
        .take(visible_height)
    {
        match item {
            ListItem::GroupHeader { name } => {
                lines.push(Line::from(vec![Span::styled(
                    format!("  {} ", name),
                    theme::header(),
                )]));
            }
            ListItem::Component {
                name,
                description,
                selected,
            } => {
                let is_cursor = i == state.cursor;
                let check_style = if *selected {
                    theme::selected()
                } else {
                    Style::default().fg(theme::GRAY)
                };
                let check_char = if *selected { "✓" } else { " " };
                let cursor_char = if is_cursor { ">" } else { " " };
                let cursor_style = if is_cursor {
                    theme::cursor()
                } else {
                    Style::default()
                };
                let name_style = if is_cursor {
                    theme::cursor()
                } else {
                    theme::unselected()
                };

                lines.push(Line::from(vec![
                    Span::styled(format!(" {} ", cursor_char), cursor_style),
                    Span::styled("[", check_style),
                    Span::styled(check_char, check_style),
                    Span::styled("] ", check_style),
                    Span::styled(format!("{:<24}", name), name_style),
                    Span::styled(description.to_string(), theme::hint()),
                ]));
            }
        }
    }

    let list = Paragraph::new(lines);
    f.render_widget(list, list_area);

    // Footer
    let selected_count = state.selected_count();
    let footer = Paragraph::new(Line::from(vec![Span::styled(
        format!("  {} selected", selected_count),
        Style::default().fg(theme::GREEN),
    )]))
    .block(Block::default().borders(Borders::TOP));
    f.render_widget(footer, chunks[2]);
}
