//! Request-scoped cancellation for synchronous Apple Events helpers. The scope
//! lives only inside one spawn_blocking closure, never across an async await.
use std::{
    cell::RefCell,
    io::{self, Read},
    process::{Command, Output, Stdio},
    sync::{atomic::{AtomicBool, Ordering}, Arc},
    thread,
    time::{Duration, Instant},
};

pub(crate) struct ScriptControl {
    cancelled: Arc<AtomicBool>,
    deadline: Instant,
    cleanup: bool,
}

impl ScriptControl {
    pub fn new(cancelled: Arc<AtomicBool>, budget: Duration) -> Arc<Self> {
        Arc::new(Self { cancelled, deadline: Instant::now() + budget, cleanup: false })
    }
    fn stopped(&self) -> Option<ScriptRunError> {
        if self.cancelled.load(Ordering::SeqCst) { Some(ScriptRunError::Cancelled) }
        else if Instant::now() >= self.deadline { Some(ScriptRunError::BudgetExceeded) }
        else { None }
    }
}

thread_local! {
    static CURRENT: RefCell<Option<Arc<ScriptControl>>> = const { RefCell::new(None) };
}

pub(crate) struct ScriptScope(Option<Arc<ScriptControl>>);
impl ScriptScope {
    pub fn enter(control: Option<Arc<ScriptControl>>) -> Self {
        Self(CURRENT.with(|current| current.replace(control)))
    }
    /// Cleanup ignores collection cancellation, but has its own five-second
    /// deadline. Nested fallback cleanup shares that deadline.
    pub fn cleanup() -> Self {
        let existing = CURRENT.with(|current| current.borrow().clone());
        let control = existing.filter(|control|control.cleanup).unwrap_or_else(||Arc::new(ScriptControl {
            cancelled: Arc::new(AtomicBool::new(false)),
            deadline: Instant::now() + Duration::from_secs(5), cleanup: true,
        }));
        Self::enter(Some(control))
    }
}
impl Drop for ScriptScope {
    fn drop(&mut self) { CURRENT.with(|current| { current.replace(self.0.take()); }); }
}

#[derive(Debug)]
pub(crate) enum ScriptRunError { Cancelled, BudgetExceeded, Io(io::Error) }

pub(crate) fn check_current() -> Result<(), ScriptRunError> {
    match CURRENT.with(|current|current.borrow().as_ref().and_then(|control|control.stopped())) {
        Some(error) => Err(error), None => Ok(()),
    }
}

pub(crate) fn run_command(mut command: Command, timeout: Duration) -> Result<Output, ScriptRunError> {
    check_current()?;
    let mut child = command.stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().map_err(ScriptRunError::Io)?;
    // Drain while the process runs: waiting for exit before reading can deadlock
    // when a document exceeds the OS pipe capacity.
    let mut stdout = child.stdout.take().expect("piped stdout");
    let mut stderr = child.stderr.take().expect("piped stderr");
    let out = thread::spawn(move || { let mut bytes=Vec::new(); stdout.read_to_end(&mut bytes).map(|_|bytes) });
    let err = thread::spawn(move || { let mut bytes=Vec::new(); stderr.read_to_end(&mut bytes).map(|_|bytes) });
    let deadline = Instant::now() + timeout;
    let status = loop {
        if let Err(error)=check_current() { break Err(error); }
        if Instant::now()>=deadline { break Err(ScriptRunError::BudgetExceeded); }
        match child.try_wait() {
            Ok(Some(status)) => break Ok(status),
            Ok(None) => thread::sleep(Duration::from_millis(25)),
            Err(error) => break Err(ScriptRunError::Io(error)),
        }
    };
    if status.is_err() { let _=child.kill(); let _=child.wait(); }
    let stdout=out.join().map_err(|_|ScriptRunError::Io(io::Error::other("stdout reader failed")))?
        .map_err(ScriptRunError::Io)?;
    let stderr=err.join().map_err(|_|ScriptRunError::Io(io::Error::other("stderr reader failed")))?
        .map_err(ScriptRunError::Io)?;
    Ok(Output {status:status?,stdout,stderr})
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn cancelled_script_is_reaped_and_next_scope_is_not_cancelled() {
        let flag=Arc::new(AtomicBool::new(false));
        let control=ScriptControl::new(flag.clone(),Duration::from_secs(10));
        let worker=thread::spawn(move || {
            let result = {
                let _scope=ScriptScope::enter(Some(control));
                let mut command=Command::new("/bin/sleep"); command.arg("10");
                run_command(command,Duration::from_secs(10))
            };
            assert!(check_current().is_ok(), "a reused worker thread must not inherit cancellation");
            result
        });
        thread::sleep(Duration::from_millis(100)); flag.store(true,Ordering::SeqCst);
        assert!(matches!(worker.join().unwrap(),Err(ScriptRunError::Cancelled)));
        assert!(check_current().is_ok());
    }
    #[test]
    fn running_process_stops_at_request_budget() {
        let _scope=ScriptScope::enter(Some(ScriptControl::new(Arc::new(AtomicBool::new(false)),Duration::from_millis(100))));
        let mut command=Command::new("/bin/sleep"); command.arg("10");
        assert!(matches!(run_command(command,Duration::from_secs(10)),Err(ScriptRunError::BudgetExceeded)));
    }
    #[test]
    fn deadline_and_cleanup_scopes_are_bounded_and_restore_cancellation() {
        let flag=Arc::new(AtomicBool::new(true));
        let _scope=ScriptScope::enter(Some(ScriptControl::new(flag,Duration::from_secs(1))));
        assert!(matches!(check_current(),Err(ScriptRunError::Cancelled)));
        { let _cleanup=ScriptScope::cleanup(); assert!(check_current().is_ok()); }
        assert!(matches!(check_current(),Err(ScriptRunError::Cancelled)));
        let _expired=ScriptScope::enter(Some(ScriptControl::new(Arc::new(AtomicBool::new(false)),Duration::ZERO)));
        assert!(matches!(check_current(),Err(ScriptRunError::BudgetExceeded)));
    }
    #[test]
    fn large_output_does_not_deadlock_in_a_full_pipe() {
        let mut command=Command::new("/usr/bin/printf"); command.args(["%0100000d","0"]);
        let output=run_command(command,Duration::from_secs(5)).unwrap();
        assert!(output.status.success()); assert_eq!(output.stdout.len(),100_000);
    }
}
