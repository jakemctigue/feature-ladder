// Compile in the fingerprint of the Python files this binary mirrors.
include!("src/fingerprint.rs");

fn main() {
    let crate_dir = std::path::PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    let root = crate_dir.join("..").join(".."); // <plugin>/hooks/guard-rs -> <plugin>
    for (rel, _) in REFERENCE_FILES {
        println!("cargo:rerun-if-changed={}", root.join(rel).display());
    }
    let fp = reference_fingerprint(&root).expect("the Python reference files are next to the crate");
    println!("cargo:rustc-env=GUARD_PY_FINGERPRINT={fp:016x}");
}
