# Changelog

What changes for you in each release. The newest is first.

A release takes its section from this file: the section for the version being
released becomes the body of its GitHub Release, and a release whose version
has no section here is refused before anything is built. Changes that have not
been released yet collect under **Unreleased**.

## 2.0.0

This is a new major version, so `@v1` stays on 1.19.0. Change
`featureflip-flag-cleanup-action@v1` to `@v2` in your workflow to get this
release and the ones after it. The CircleCI orb moves to 2.0.0 for the same
reason: its default image is now `:v2`.

### Changed

- The transform engine's lines in the job log (`File: … with N rewrites`,
  `Rewrites used the following rules: …`) are now labelled
  `featureflip.engine` instead of `polyglot_piranha`. If a log filter of yours
  matches the old name, update it.
- **Breaking:** the outcome reported when the transform engine rejects its own
  rewrite is now `[engine-error]`, renamed from `[piranha-error]`. It still
  fails the run with exit code `1`. Update any script or log search that
  matches the old label.

### Fixed

- **C#:** a few values were given the wrong static type when proving a removal
  safe, which could let a removal through that the C# compiler binds
  differently: `list.ToArray()` (an array, not the list), a call that no
  method of your type with that name can take (an extension method binds
  instead), and a local declared after a label (`L: object o = …;`), which
  hides a field of the same name like any local.

- **C#:** a setting of an options class shared with other settings is no
  longer kept by its binding when the section is held in a local
  (`var section = configuration.GetSection(…)`), when names are qualified by
  the class's namespace, or when a `PostConfigure<T>(o => …)` sets the other
  settings from subsections of other names.

## 1.19.0

### Added

- **TypeScript and JavaScript:** when the removal leaves every use of a
  component in the file passing the same `true` or `false` to a prop
  (`<Row showExpiry={true} />`), the prop is removed: from each element, from
  the component's destructured parameter and from its props type, and what it
  guarded inside the component is simplified. This applies to a component that
  is not exported and is used only as a JSX element in its own file, whose
  props type is its own. A prop that already received that literal before the
  run is left alone.
- **TypeScript and JavaScript:** a prop the removal leaves unread inside such
  a component (`showExpiry && isExpired(expiresAt)` folding to `false` leaves
  `expiresAt` passed and never used) is removed the same way: from each
  element, the destructured parameter and the props type. A value passed to
  it that could do work (a call) keeps the prop.
- **TypeScript and JavaScript:** removing a prop every element passes as the
  same `true` or `false` now reaches an exported component, when every file
  that renders it imports it by name from the file that declares it and does
  nothing else with it. The prop goes from the component and from every
  element, in each of those files. A component that is re-exported, imported
  under another name or as a namespace, or mentioned any other way is left
  alone.
- **TypeScript and JavaScript:** a parameter of a function that is not
  exported and is only ever called by name in its own file is removed when
  every call now passes it the same `true` or `false` the removal wrote
  (`formatRow(name, true)`): from each call and from the function, and what
  it guarded inside is simplified. A parameter the removal leaves unread in
  such a function goes the same way, with the value each call passed for it,
  unless a call passes something that could do work.
- **C#, Java, Kotlin, Python, TypeScript and JavaScript:** a test assertion
  the removal turns into one that always passes (`Assert.True(true)`,
  `assertTrue(true)`, `expect(true).toBe(true)`, `assert True`) is deleted,
  and listed in the pull request with the deleted mock lines. Only an
  assertion the removal itself produced is touched: one you wrote that way
  stays. An assertion the removal turns into one that always FAILS
  (`Assert.True(false)`) is not edited; the pull request names it so you can
  delete or rewrite the test.

### Changed

- A comment directly above a statement the removal deleted outright (an
  inlined declaration, a guard of the removed branch) no longer stays behind
  unmentioned. When it describes the flag's state ("while the flag is off")
  it is deleted with the statement; otherwise it is listed under "comments
  describe behaviour this pull request removed". A comment separated from the
  statement by a blank line, a doc comment, and one that may also describe
  the next statement are left alone.
- A comment that says "the flag" or names a local the removal inlined away
  is now reported when it sits above the changed code in the same block, not
  only on or directly above the changed line. Nothing is edited.
- **TypeScript and JavaScript:** a file whose only mention of the removed
  flag's key is a string element of an array that also holds a computed
  value, such as a query cache key `['scheduled-changes', orgId] as const`,
  is now listed in its own informational group in the pull request body
  instead of under "still reference this flag". Nothing is rewritten. A list
  of nothing but string keys, an array handed straight to a call, and a
  template string still get the warning.

### Fixed

- **C#:** a string a test passes to a private helper that only binds it as a
  parameter of interpolated SQL (`ExecuteSqlAsync($"… {payloadJson} …")`) no
  longer keeps an option of the same name in place, even in a file that
  compares strings ignoring case.

- **C#:** an options class that declares a constant other than a string
  (`public const long MaxBytes = 64 * 1024;`) no longer keeps its removed
  setting in place, when the constant's value is only literals and operators.

- **C#:** a Terraform file that names a removed type or options class only
  in a comment no longer keeps it. The comment is listed in the pull request
  with the other prose. A name in a value or a heredoc still keeps it.

- **C#:** a read through a local created as a class of yours that derives
  from a library class (`using var factory = new MyAppFactory(); …
  factory.Gate.Enabled`) no longer counts as possibly going through a member
  the library class supplies, when the member read is one your class
  declares. Before, it kept a gate family's options setting in place.

- **C#:** a configuration key of another section that ends in a removed
  option's name (`configuration.GetValue("Quartz:Enabled", true)`, the option
  bound to section `Scheduled`) no longer keeps that option and its
  configuration key.

- **C#:** a string that matches a removed option's name (for example
  `nameof(Feature.Enabled)`) no longer keeps that option and its
  configuration key. This applies when the string is in a file whose every
  by-name lookup goes through `typeof(T)` of an unrelated type, or passes only
  constants that name something else (`Entry(e).Property(nameof(E.Date))`).

- Every file path in a pull request body, in a dry run's diff and in the
  run's report is now written relative to the repository root. A run over
  several directories used to mix three spellings (from the root, from the
  swept directory, and absolute), and a dry-run diff carried absolute headers
  that `git apply` rejects; it now applies from the checkout root.
- A file whose every comment naming the flag is already listed under what
  that comment describes is no longer listed again, with no lines under it,
  under "mention this flag only in comments".
- **TypeScript and JavaScript:** in a file holding JSX, a named import the
  removal left unused is now removed even when its `import { … }` keeps other
  names, if the file's compiler config sets `"jsx": "react-jsx"` or
  `"react-jsxdev"` and the file has no `@jsx` pragma. It used to be kept,
  which fails the build under `noUnusedLocals`. Under the classic runtime, or
  when the runtime cannot be told, such imports are still kept, since an
  element can call one of them without naming it.
- **C#:** a `typeof(X)` no longer keeps `X`'s constructor parameter when every
  value made from it in its file only reads names, flags and attributes. That
  holds through private fields, loops, LINQ lambdas, method groups and private
  helpers, as in a structural test over a `private static readonly Type[]` of
  controllers that checks each one's attributes. Anything that could construct
  the class or hand the value elsewhere still keeps it.

- **C#:** a target-typed `new(…)` that passes a literal where a constructor
  takes a class no literal converts to (a class or struct of your repository
  with no conversion operator, or a framework type such as `TimeProvider`,
  `HttpClient` or `ILogger<T>`) is no longer taken for a possible call to it.
  `new(null, 1)` beside a constructor taking `(IGate gate, TimeProvider
  clock)` used to keep the whole gate family's leftovers.

- **C#:** a constructor parameter is no longer kept just because your
  repository has another type of the same name in another namespace (two
  `OrdersController`s, say). Each spelling of the name is looked up
  the way C# does, through the file's namespaces, `using` directives,
  global usings and `<Using>` items, and spellings of the other type are
  left alone. A target-typed `new(…)` is looked up from the type its
  context spells (`private OrdersController Make() => new(…)`). A spelling
  that could name either still keeps the parameter.

- **C#, Kotlin, Swift, Go, Ruby and Python:** an inlined local whose
  declaration sat directly under a comment no longer leaves its `;` at the
  end of that comment (`// Read once per run.;`).
- **C#, Java, TypeScript and JavaScript:** a kept branch that declares a
  local is now unwrapped into the surrounding code, as a branch without one
  already was, when nothing else in the enclosing method or function uses
  that name. It used to stay as a bare `{ … }` block. A branch holding a
  `using` declaration keeps its block, since the block decides when the
  resource is disposed.
- **TypeScript and JavaScript:** a destructured name the removal left unused
  is dropped from its pattern when the pattern keeps another name
  (`const { orgId, tab } = useParams()` becomes `const { tab } =
  useParams()`). When it was the pattern's only name and the value is a
  call, the call is kept as a statement, as it already was for
  `const orgId = useParams()`; a type argument that spells the name
  (`useParams<{ orgId: string }>()`) no longer keeps the whole declaration.

- **C#:** a test host's registration through its own helper,
  `Replace<IGate>(services, new StubGate(enabled: true))`, now goes with the
  gate family when the fake is built from literals by a constructor that only
  stores them, also inside a lambda. When the family's leftovers are kept,
  the pull request now lists every reason, not only the first one found.

- **C#:** a test whose only subject is the removed gate's type, every
  statement an assertion naming it in `typeof(…)` (for example, that a class's
  constructors never take the gate), is now deleted with the gate family and
  listed in the pull request. Before, it kept the family's empty interface and
  classes in place.

- **C#:** a test class whose every test the removal deleted is now deleted
  too, with its file when nothing else is declared in it, even when the gate
  family's other leftovers are kept. Before, the pull request shipped the
  empty class. A class with an attribute, a base class or another part is
  kept.

- **C#:** a private helper's parameter of the removed gate's interface that
  only registers it (`services.AddScoped(_ => gate);`) is now removed with
  those registrations and every caller's argument, and the test doubles passed
  for it then go with the gate family. Before, it kept the family's empty
  interface and classes in place. A named argument (`gate: mock.Object`) no
  longer keeps a test double either.

- **C#:** a static helper's parameter of the removed gate's interface that the
  removal left unread is now removed, together with the argument every caller
  passes for it. The field or constructor parameter that a caller passed in
  then goes with the rest of the gate family. Before, those leftovers kept the
  family's empty interface and classes in place.

- **C#:** a gate family's leftovers now also go when they reach a primary
  constructor (`class Job(IGate gate, …)` whose `gate` the removal left
  unread), a test local built as a fake and only passed to such a
  constructor, or a test harness's public auto-property holding a fake that no
  other file names.

- **C#:** `attributes.Add(new(…))` on a local `List<T>` (declared, `var` from
  a method of the same class, or `var rows = new List<T>()`, also from inside
  a loop or lambda) is read as creating a `T`, so it no longer
  counts as a possible call to an unrelated constructor. Neither does a
  property initializer `public HashSet<string> Names { get; } = new(comparer);`,
  which creates the property's type.

- **C#:** a test's Moq field built by a target-typed `new()`
  (`private readonly Mock<IGate> _gate = new();`) now counts as a double of the
  gate, like one built with `new Mock<IGate>()`. Before, it kept the gate
  family's empty interface and classes in place.

- **C#:** a statement that only asks the container for the removed gate's
  interface, `scope.ServiceProvider.GetRequiredService<IGate>();` (what the
  removal leaves of `var gate = …` once nothing reads `gate`), now goes with the
  gate family. Before, it kept the family's empty interface and classes in
  place. The container then no longer creates the gate at that point.

## 1.18.1

### Fixed

- A Featureflip organization that is read-only because it is over its plan's
  limits is no longer reported as a token problem. When linking a removal PR
  to its flag is refused for that reason, the run says so once, stops trying to
  link for the rest of the run, and the PRs open as usual.

## 1.18.0

### Fixed

- **C#:** more of the constructor parameters a removal leaves typed as a
  deleted interface are now removed instead of kept with a finding. A
  target-typed `new(…)` elsewhere is no longer taken for a possible caller
  when it passes a `const` of a built-in type where that constructor takes
  the interface, or when its own type is clear from where it stands: assigned
  to a local, parameter, array element or `List<T>` element of a declared
  type, returned from an operator or conversion operator, or set by an index
  initializer of a `Dictionary<K, V>` or `List<T>`.

- **C#:** the gate class's own unit test, which builds the class and reads
  the member (`var gate = new FeatureflipGate(client); Assert.Equal(flagValue,
  gate.IsEnabled);`), is now deleted with the member, as a test fake's test
  is. Before, the read was replaced with the kept value, leaving
  `Assert.Equal(flagValue, true)`: an analyzer error that broke the build, and
  a failing row. Reads through the class outside test methods are still
  replaced.

- **C#:** a test helper whose `bool` parameter defaults to the value being
  removed, called somewhere without an argument, is now left as it is and
  your gate kept. Such a call never chose the removed value and may depend on
  it, so rebuilding it with the kept value could leave a failing test. Before,
  the call ran with the kept value and was listed for review. A test that
  passes the removed value itself to a public helper is now deleted, as for a
  private one.

- **C#:** a read of a member named like your gate's, through a value whose
  type can't be a gate, no longer keeps the gate in a file that also holds
  one. The value's type is followed through locals, your types' declared
  fields, properties and methods (base classes included), `new`, `await`,
  and the LINQ and EF Core operators that keep their source's element type
  (`(await h.Context.Subscriptions.SingleAsync(s => …)).IsEnabled`). A
  projection such as `Select`, `dynamic`, or an extension method of the
  operator's name in your code still keeps it.

- **C#:** a string naming the gate's member passed to a private helper of
  the same class, which only uses it as a key into such a value
  (`ex.Errors[field]`) or in a test framework's value assertion, no longer
  keeps your gate.

- **C#:** a test that builds its fake at the removed value and switches it
  to the kept one halfway (`var gate = new StubGate(enabled: false); …
  gate.Enabled = true;`) now loses its removed-value half. The fake is built
  with the kept value, and a local built from it in between, such as
  `var job = new Job(gate, …)`, stays, as does one read from a property that
  only reads a field or throws (`factory.TenantContext`). The other way round, a fake built at
  the kept value and switched to the removed one (`var stub = new
  StubGate(true); … stub.Enabled = false; …`), loses the write and what
  follows it, as a fake set up in a field already did. Before, any
  construction in such a test kept the gate.

- **C#:** a constructor argument that builds a class whose constructors only
  assign their parameters (`new StubGate(enabled: true)`) no longer keeps an
  unread constructor parameter. Before, such an argument counted as one that
  may do work.

- **C#:** a private test helper whose `bool` parameter builds a test fake and
  is also passed on to a public static helper building the same fake
  (`TestExpiry.Create(ctx, expiration: gateOn)`) is now pinned. That argument
  takes the kept value, which the public helper's own rebuild then removes.
  Before, the second use of the parameter kept your gate.

- **C#:** a statement whose comment or string happens to hold a mocking
  word ("checked every tick", `"Returns"`) is no longer taken for a mock's
  setup. Before, a gate read in it kept your gate as a test double.

- **C#:** a string naming the gate's member in a file that holds the gate no
  longer keeps it when it is only part of an exception thrown with nothing
  but constants (`throw new ValidationException(new[] { new
  ValidationFailure("IsEnabled", "IsEnabled is required.") })`). A by-name
  lookup's own name argument still counts.

- **C#:** a mock whose name a lambda's body also reads in the same file
  (`services.AddScoped(_ => gate)` in a helper) is now resolved. Before, its
  `Setup` lines stayed and kept your gate.

- **C#:** a test class whose constructor only stubs the kept value
  (`public Tests() => _gate.SetupGet(g => g.IsEnabled).Returns(true);`) now
  loses that constructor, when it is the class's only one. Before, the stub
  kept your gate.

- **C#:** a test phase run at the removed value is now cut even when it
  builds an object whose initializer sets a member named like something the
  rest of the test reads (`new HttpRequestMessage(…) { Content = … }`, then
  `res.Content`). Before, the whole test stayed and kept your gate.

- **C#:** a string naming the gate's member passed to a test framework's
  value assertion (`Assert.Contains("IsEnabled", ex.Errors.Keys)`,
  `Assert.Equal`, `Assert.AreEqual`, …) no longer keeps your gate, when
  `Assert` can only be the framework's class.

- **C#:** a gate read through a primary-constructor parameter of a class
  that implements a library interface (`class Job(IGate gate) : IJob`) is
  now proven. Before, the library base was taken to maybe declare a member
  of the parameter's name, and the read kept your gate. A library base spelled
  like a class (`JobBase`, `IOException`) still does.

- **C#:** a file is no longer taken to reach a gate just because it calls
  some `Create(…)` when a static helper of that name returns something
  holding the gate (`TestProtectors.Create()`). Such a static member is
  reached only through its class's name, which the file would spell. An
  instance or extension method of the name, or a `using static`, still
  counts.

- **C#:** a test harness from a static factory that returns its own class
  (`using var h = Harness.Create();`) is now typed like `new Harness()`, so
  a fake's knob written through it (`h.Gate.Enabled = false`) is resolved.
  Before, the write kept your gate.

- **C#:** fewer target-typed `new(…)` calls elsewhere keep an unread
  constructor parameter. One in a project that can't reference the class no
  longer counts for a primary constructor either, nor does one passing a
  literal (`new(Limits.Projects, 1, 3)`) where the constructor takes an
  interface your code declares. Before, either kept the parameter.

- **C#:** a constructor parameter of a type the removal deletes is now
  removed when some file builds a class with a target-typed `new(…)` in a
  conditional whose other branch is `null`, `default` or another `new(…)`
  (`dto is null ? null : new(dto.Id, …)`). Before, that `new(…)` kept the
  parameter, whatever class it built.

- **C#:** a public instance helper of a `sealed` test class whose `bool`
  parameter builds a test fake (`h.CreateHandler(planGated: true)` on a
  harness from `using var h = Harness.Create()`) is now rebuilt like a public
  static one. The fake is built with the kept value, the parameter and every
  argument go, and each test that passed the removed value is listed for
  review. Calls on another class's method of the same name are left alone. A
  `<paramref>` naming the parameter in either kind of helper's doc comment
  becomes `<c>…</c>` instead of keeping your gate.

- **C#:** a pinned test helper whose every call keeps its test (each passes
  the kept value, or the default rebuilds it) is now edited. Before, its
  parameter, arguments and fake stayed as they were, and the fake kept your
  gate's member.

- **C#:** more of an emptied gate family's leftovers are now removed instead
  of keeping the interface and its fakes in place:
  - a test fake that also serves other gates (`class OnGate : IGateA, IGateB`)
    loses the removed gate's interface from its base list, and its
    registration for that interface goes;
  - two private fakes of the same name nested in different test classes both
    go;
  - a registration whose factory passes only literals
    (`services.AddScoped<IGate>(_ => new FixedGate(true))`) goes;
  - a `new(…)` whose context names the consumer being rebuilt (`Handler
    Make() => new(…)`) loses its argument like `new Handler(…)`;
  - a C# file of a project that cannot see yours, which does not parse or
    builds things with `new(…)`, no longer keeps a constructor parameter.

- **C#:** a private static test helper whose `bool` parameter builds a test
  fake no longer keeps your gate just because its body calls another method of
  the same name on a value. `BuildServiceProvider(…, bool planGated = false)`
  ending in `return services.BuildServiceProvider();` is now pinned like any
  other helper.

- **C#:** a test whose call to a pinned private helper gets the value being
  removed only through the parameter's default (`BuildServiceProvider(cs)` with
  `planGated = false`) is no longer deleted. It runs with the kept value, and
  the pull request lists it for review, as for a public helper. A call that
  passes the removed value explicitly still deletes its test.

- **C#:** a read through a dependency-injection lookup used directly
  (`Assert.True(sp.GetRequiredService<IGate>().IsEnabled)`) is now rewritten
  like one through a `var` it initializes, instead of keeping your gate. Lookup
  reads also stay proven in a repository where some C# file does not parse
  but only *calls* `GetRequiredService<…>()`. Before, any such file spelling the
  name switched the proof off for every file.

- A request to the Featureflip or GitHub API that stalls or drops no longer
  fails the run on its own. Each read now waits up to 30 seconds for an answer
  rather than 5, and one that times out or loses its connection is tried again
  twice, after 1 and then 4 seconds. A write is never repeated, since one that
  timed out may still have landed. An error the API answered with is reported
  as before.

- A symlink that points at itself, or into a loop of links, no longer stops
  the run. A checkout holding one (`node_modules -> node_modules`, say) failed
  every flag with `Symlink loop from …` when the link had no file extension or
  a source one. Such a link names no file, so it is now skipped like any other
  file nothing can read.

- **C#:** a wrapper member could be removed while a controller still read it
  through MVC's `ViewBag`. `ViewBag` is a library member typed `dynamic`, so a
  file that stored your gate in it (`ViewBag.Gate = _gate;`) could hand it to
  another controller that read `ViewBag.Gate.IsEnabled` while spelling none of
  your gate's types. That read was not counted. Now it is judged like one in
  a file that names your gate. This covers a read inside a class with a
  library class among its bases (directly or through bases of your own, a
  class nested in one included) whose receiver may start at an inherited
  member. It also covers a read anywhere through a member such a class of
  yours inherits (`_inner.ViewBag…`, `new HomeController().ViewBag…`, but
  not `_db.Flags…` when your context declares `Flags`). A read through a
  lambda's parameter or a declared variable of another type is cleared as
  before, and so is a base named like an interface (`I` and a capital).

## 1.17.0

### New

- **TypeScript and TSX:** OpenFeature's React SDK. `useBooleanFlagValue("KEY",
  default)` from `@openfeature/react-sdk` is removed like `useFeatureFlag`,
  including through a key constant, a key imported from another file, and a
  hook of your own that wraps it. `client.getBooleanValue("KEY", default)` is
  removed in a file importing `@openfeature/react-sdk` when `client` is what
  that file got from `useOpenFeatureClient()`, as it already was in a file
  importing `@openfeature/web-sdk`. A client from one of your own modules is
  left alone there, since it may be a server client. A file that also mentions any other `@openfeature/…`
  package, a server client included, is still left alone. `useBooleanFlagDetails`
  and `useFlag` return details rather than a value and are not rewritten. The
  import a removal strands is kept as `import "@openfeature/react-sdk";` unless
  you list the package in `side-effect-free-modules`.

- **TypeScript and JavaScript:** a value the removal shortened goes back onto
  the line of its `=` or `:` once it fits. `const pollEnabled =\n
  onboardingEnabled && !dismissed;` comes out as `const pollEnabled =
  !dismissed;`, and the same goes for an assignment, an object property and a
  class field. A JSX element written over several lines goes back onto its
  tag's line the same way: `<Provider\n  flags={{ a: true, b: false }}\n>`
  with `b` removed becomes `<Provider flags={{ a: true }}>`. Only a layout the
  line's length needed is joined: a value you wrapped although it fit, one
  that still spans lines, and one with a comment where it breaks are left as
  they are. The width is the one the list joins use.
- **TSX and JavaScript:** a fragment the removal leaves directly inside
  another fragment or an HTML element, as `{flag && (<>…</>)}` folds to, is
  replaced by its children, re-indented. Under a component (`<Menu>`), a
  fragment holding text, and a `React.Fragment` with a `key` stay as they
  are, as do `<option>`, `<textarea>` and `<title>`.

- **TypeScript and JavaScript:** a JSX element whose attributes the removal
  deleted no longer leaves its `>` on a line of its own. When an emptied or
  default-equal prop is dropped from an element written one attribute per
  line, `<TestProvider\n  flags={{}}\n>` comes out as `<TestProvider>`,
  and `<Panel\n  a={1}\n  b\n/>` with `b` removed as `<Panel a={1} />`.
  The conditions are the ones the parameter and argument lists follow: every
  attribute left on one line, no comment inside the element, and a joined
  line that fits your Prettier `printWidth`, your `.editorconfig`
  `max_line_length`, or, with neither, the file's longest line. A Prettier
  configuration that sets `singleAttributePerLine` now joins nothing, lists
  included.
- **TypeScript and JavaScript:** an argument equal to the callee's default is
  now left out when the callee is a test helper declared inside a block, such
  as `const renderWithProviders = (ui, flags = {}) => …` inside
  `describe(…, () => { … })`. `renderWithProviders(<Page />, {})` comes out
  as `renderWithProviders(<Page />)`. The helper must be bound once, to a
  function, in a block that encloses the call, and never re-assigned. An
  unexported top-level `let` helper is followed the same way as a `const`.
- **Every language:** a file that names the flag's key only as a URL path
  segment in a string, such as
  `` fetch(`/environments/${id}/scheduled-changes`) `` or
  `[HttpPost("{id}/scheduled-changes")]`, is now listed in its own ℹ️
  paragraph of the pull request instead of under "still reference this
  flag". An endpoint named after its feature used to make a complete
  removal look unfinished. Nothing about what is rewritten changes.
- **Every language:** when `directories` leaves part of the checkout out,
  the pull request now names the files there that also mention the flag,
  in an ℹ️ paragraph ("N file(s) outside the swept directories also name
  this flag; not examined"). Before, a script or `README` outside the
  configured directories that still named the key went unmentioned, and the
  pull request read as if nothing was left. Nothing outside `directories` is
  edited.
- **C#:** a test fake whose one constructor stores its `bool` argument in the
  knob its member reads (`public StubGate(bool enabled) => Enabled = enabled;`)
  no longer refuses its gate family as `sibling-not-inert`. Each construction
  is judged by its argument like any other knob setting: `new StubGate(true)`
  with the kept value stays, a test building it with the removed value is
  deleted, and a construction passing a variable keeps the wrapper.
- **Every language:** a comment beside the change that describes the branch
  the removal kept ("the member lookup only runs while the flag is on", when
  the flag serves `true`) is now listed in its own ℹ️ paragraph, since its
  condition is gone and the code it describes now always runs. It used to
  sit in the general list of flag-state comments, or, in a comment of
  several lines where another line named the flag, not be listed at all.
  Comments are still never edited.

- **TypeScript and JavaScript:** a new input, `side-effect-free-modules`,
  takes module specifiers whose load does nothing, spelled exactly as your
  imports spell them (`@/lib/featureflip, ./keys`). An import the removal
  left with nothing to import is deleted instead of being kept as a bare
  `import "…";`, and a CommonJS `require` likewise. Since this rests on your
  word rather than a proof, the pull request names each import deleted this
  way. The default is empty, which changes nothing.

### Fixed

- **C#:** a `[Theory]` writing its own `bool` parameter to a test fake's knob
  (`_gate.Enabled = on;`, the rows saying `on`) no longer keeps the gate. Each
  row passing the value being removed is deleted, the column and the
  parameter go with the write, and a theory left with no row goes whole.

- **C#:** a public static test helper whose `bool` parameter builds a test
  fake, called from other files (`TestExpiry.Create(ctx, expiration: false)`,
  `TestExpiry.Off(ctx)`), no longer keeps the gate. The fake is built with the
  kept value, the parameter and every call's argument go, and no test is
  deleted: each one that passed the value being removed is listed in the pull
  request for review.

- **C#:** a receiver read with a member none of your gate's types declares
  is proven no gate even when its type comes from a library
  (`var config = await response.Content.ReadFromJsonAsync<Dto>()`) or an
  untyped lambda parameter (`c => c.IsEnabled && c.Strategy == 1`), and when
  that member is passed as an argument (`Assert.Equal(key, config.Key)`) or
  compared with `==`/`!=`. These assume no library hands one of your types
  back as `dynamic`, as the per-member proof already did. The argument and
  comparison cases also require that no file of yours declares or calls a
  method of that name. A `foreach` variable is judged like any other local; any
  `foreach` in a member used to leave its name unproven.

- **C#:** a read through a receiver also read with a member none of your gate's
  types declares is proven again in a repository where some C# file does not
  parse, unless that file may declare one of the types or a type they inherit
  from. A `dynamic` in a comment or a string no longer counts as one, and a
  file outside every project no longer counts just because some project
  compiles a file from outside its own folder.

- **TypeScript and JavaScript:** in a file importing `@openfeature/web-sdk`,
  a `getBooleanValue` read on a client imported from one of your own modules
  was rewritten too. If that client was a server client, its read was a
  promise, and the result (`true.then(...)`) failed at runtime. The read is
  now rewritten only when the same file binds the client to
  `OpenFeature.getClient()`, with `OpenFeature` imported from
  `@openfeature/web-sdk` under that name. Other reads are left alone, and the
  pull request names the file as still referencing the flag.

- **C#:** one test or method that names your gate's interface no longer makes
  every unrelated `IsEnabled` in the same file block the gate's removal. Each
  member is judged on its own text: one that names nothing leading to the
  gate (an Entity Framework row's `config.IsEnabled`, say) is not counted.
  A file of your repository outside every project (a test fixture, say) no
  longer counts for whether the repository uses `dynamic`.

- **C#:** two ways a wrapper member could be removed while a file still read
  it. A file that spelled none of your gate's types could take one from a
  member typed `dynamic` in another file (`var x = D.Get(); x.IsEnabled`), or
  call a static wrapper's member by its bare name under a `global using
  static` (or a project `<Using Static="true">` item). Such a read was not
  counted, and the removal broke the build. A member typed `dynamic` now
  counts like one returning your gate's interface, and a `global using
  static` naming one of your gate's types means no file is cleared just for
  the names it spells.

- **C#:** a project that compiles a file from another project's folder
  (`<Compile Include="..\Other\Stub.cs" Link="Stub.cs" />`) no longer makes
  every gate in the repository count as published. The file now counts as
  compiled by both projects. A link the tool cannot resolve, through a
  property or a wildcard, still makes every project count as reachable.

- **C#:** a target-typed `new(…)` is read by the type its context declares.
  A property `Dictionary<string, string> Txt { get; } = new(comparer)` no
  longer counts as a possible construction of a test fake built through its
  constructor's `bool`, and a test helper `Svc Make() => new(_fake)` hands the
  fake to the code under test rather than letting it escape.

- **C#:** a private test helper whose `bool` parameter builds a test fake
  (`private static Policy Make(bool enabled = true) => new(new
  StubGate(enabled));`) is resolved like one whose parameter chooses a stub:
  each test calling it with the value being removed is deleted, the others
  lose the argument, the parameter goes, and the fake is built with the kept
  value. A helper passing its own `bool` parameter on to such a helper
  (`Make(bool enabled = true) => Build(enabled)`) is resolved the same way, by
  its own calls. A call passing the kept value no longer has to be in a plain
  test: one in a `[Theory]` just loses the argument.

- **C#:** a test fake whose member is a get-only auto-property set from its
  primary constructor (`class FixedGate(bool on) : IGate { public bool
  IsEnabled { get; } = on; }`) is now handled like one returning `=> on`. A
  target-typed `new(…)` that cannot build a fake (it passes the wrong number
  of arguments, or sits outside the type a private fake is nested in) no
  longer keeps the gate.

- **C#:** an xUnit `[Theory]` that passes its own `bool` parameter to a test
  helper choosing the flag's stubbed value (`[InlineData(true, "new")]
  [InlineData(false, "old")] … Make(on)`) no longer keeps the gate. The rows
  for the value being removed are deleted, that column and the parameter go
  from the rest, and a theory with no row left is deleted. The pull request
  lists each one.

- **C#:** a test fake that serves several gates with one member
  (`class OnGate : ICheckoutGate, IWebhooksGate { public bool IsEnabled =>
  true; }`) no longer blocks removing each gate. When the member already
  returns the value the flag keeps serving, it is left as it is for the other
  gates, and the pull request lists it.

- **C#:** a class that takes your gate's interface through a constructor
  (`public Job(IGate gate)`, the usual dependency-injection shape) no longer
  stops every file that names the class from being cleared. Those files used
  to need a proof for each unrelated `IsEnabled` they read, such as one on an
  Entity Framework row, and when the proof failed the gate was left
  unremoved.

- **TSX and JavaScript:** a comment inside a folded `{...}` in JSX, as in
  `{/* why */ flag && <Panel />}`, is no longer dropped with the braces. Such
  a container is now left as it is.

- **C#:** a test that set a fake's knob to the value being removed and then
  handed the fake to code that sets it back (a helper method, a lambda) was
  deleted as a test of the removed branch, though it ran the kept one. When
  any test code in your repository writes the fake's knob outside a test
  body, a test is now decided by such a write or construction only if the
  fake stays within the test (written there, read, or passed to an object
  being constructed); otherwise the test and the wrapper are left for you.

## 1.16.0

### New

- **TypeScript and JavaScript:** a test about the branch being removed is now
  deleted even when it never sets the flag, as long as your flag registry
  declares that branch as the flag's default
  (`'my-flag': { default: false }`) and the test's title names the flag by a
  word of its key (`hides the GitHub link when the GitHub flag is off` for
  `web-social-login-github`). Such tests used to survive the removal and fail,
  since the code they asserted against was gone. Whenever the default was the
  removed branch, the pull request now also warns that tests leaving the flag
  unset run the kept branch from now on, and asks you to run the suite before
  merging.
- **TypeScript and JavaScript:** a removed constructor parameter now leaves
  its lists on one line too. When the removal deletes a constructor parameter
  the flag left unread, together with the argument each caller passed for it,
  and either list was written one element per line with one element left,
  `constructor(\n  private readonly log: Logger,\n) {}` comes out as
  `constructor(private readonly log: Logger) {}` and
  `new ReportService(\n  logger,\n)` as `new ReportService(logger)`. The
  conditions are the ones the helper parameters and their arguments already
  follow: two or more elements before the run, no comment inside the
  brackets, and a joined line that fits your Prettier `printWidth`, your
  `.editorconfig` `max_line_length`, or, with neither, the file's longest
  line.

## 1.15.0

### New

- **Dart:** a plain `import 'package:featureflip/featureflip.dart';` that a
  removal leaves unused is now removed, so `dart analyze` no longer fails on
  `unused_import`. Before, it was always kept, and a private field whose type
  was its last use (`final FeatureflipClient _client;`) was kept with it and
  named in the pull request. Now both go. What the Featureflip Dart SDK
  exports was read from every published version of the `featureflip`
  package, and none of them declares an extension, so nothing can rely on
  the import without naming something it exports. This applies only when the
  package's `pubspec.lock` resolves `featureflip` to one of those versions
  from pub.dev, and nothing overrides it. Without a lock, with a path, git or
  other-server source, or on a newer release, the import and the field stay
  as before.
- **TypeScript and JavaScript:** `@featureflip/edge`, the Cloudflare Workers
  SDK, is treated like the other Featureflip JavaScript SDKs. It does nothing
  when it loads, so an import of it that a removal leaves unused is deleted
  instead of being kept as a bare `import "@featureflip/edge";`.
- **TypeScript and JavaScript:** a test that stays because it tests the
  branch being kept no longer keeps a title describing a flag that is gone.
  When the title names the flag by its key in the kept state
  (`renders the summary when the my-flag flag is on and the cart has items`),
  the flag's condition is dropped from it
  (`renders the summary when the cart has items`). Only a fixed set of title
  shapes is rewritten, and only in files the run changed; any other title is
  reported as before. The pull request lists each rewrite, old title and new.
- **Every language:** the pull request now lists comments next to the code it
  changed that describe a flag's state without naming the flag, such as
  `// Skips polling while the flag is off.` above the line the fold
  rewrote. Before, only comments naming the key were reported. This is a guess
  from the wording ("the flag is on", "flag is disabled", "while the flag",
  "when the flag"), so these comments are listed and never edited.
- **TypeScript and JavaScript:** a parameter or argument list written one
  element per line is put back on one line when the removal leaves it with a
  single element. `function renderChecklist(\n  steps: number\n) {` now
  comes out as `function renderChecklist(steps: number) {`, so you no longer
  join it by hand in review. It applies to the helper parameters, their
  arguments, and the default-valued arguments this tool removes. A list is
  joined only when it had two or more elements before the run, holds no
  comment, and the joined line fits your Prettier `printWidth`, your
  `.editorconfig` `max_line_length`, or, with neither, the file's longest
  line. See README's known gaps for the details.
- **Every language:** a reported comment that describes the branch this pull
  request deleted ("the flag is off" when the flag serves `true`) is listed
  first, under its own heading, because it is now false rather than just out
  of date. That covers kept comments that name the flag too. A comment that
  hedges ("even when the flag is off") is not counted as one.
- **Ruby and ERB:** a keyless wrapper that is a Rails helper is now inlined
  where your views call it (`<% if new_checkout? %>`, `<%= new_checkout? %>`),
  and deleted once nothing else names it. It must be an instance method of a
  module in `app/helpers/` of a Rails application, named as Rails derives it
  from its file, and the template must be under that application's
  `app/views/`. Calls inside a block, a bare name without `?`, `!` or
  parentheses, and a statement tag whose value nothing uses are left alone and
  reported. Nothing in the repository may spell `include_all_helpers`,
  `clear_helpers` or `helpers_path`. Any other template naming a wrapper keeps
  it, as before (`other-language`). See README, "Wrapping the SDK".
- **Dart:** a private field set by a constructor is now removed when a caller
  passes it a variable or a Flutter widget's field. Before, that kept the
  field and its parameter and named them in the pull request. Two shapes are
  covered. The first is a variable declared in the caller's own file, either
  top-level or a `static` field of the enclosing class, whose initializer
  only builds a value (`final client = FeatureClient();` passed as
  `Svc(client)`). The same goes for a field built from one
  (`final _flags = Flags(client);`). Dart initializes such a variable when
  it is first read, and that initializer does nothing else. The second is
  `widget.client` inside a `State<CheckoutPage>` subclass, when `client` is
  a plain public field of the one `CheckoutPage` in the repository, typed
  with a class of the repository or of the Featureflip Dart SDK, and nothing
  in the repository declares a `client` or `widget` getter or a
  `noSuchMethod`, subclasses the state, or subclasses, implements or mocks
  the widget. Any other property
  (`this.widget.client`, `widget?.client`, `config.client`) is still kept,
  and so are a `State` with a `with` clause and a file that imports another
  package without a prefix or a `show` list: what it brings cannot be read,
  and a top-level `widget` it declared would be the one `widget.client`
  reads. See README, "Supported Dart/Flutter flag-read shapes".

### Fixed

- **Ruby:** a wrapper call whose argument is a name an earlier, already closed
  block bound (`items.each { |u| … }` then `Flags.on?(u)`) is no longer
  inlined. That name is a method call there, which inlining would drop
  (`argument-has-call`).
- **Dart:** a flag value held in a local and read in a string as `'$enabled\$'`
  (a template followed by an escaped dollar sign) is now inlined, giving
  `'${true}\$'`. The escape used to be read as part of the name, so that read
  was left behind and the whole flag was refused with "a string template still
  reads a variable the rewrite deleted".

## 1.14.0

### New

- **TypeScript and JavaScript:** a stranded import is no longer left behind as
  a bare `import "./m";` when the file's other imports already load everything
  in that module's graph that does work. A barrel that loads a store or a
  client the same file reaches anyway is the usual case. Only imports the
  compiler keeps count, and other files' imports never do. See README's
  caveats for the rules and the one ordering change it can make.
- **TypeScript and JavaScript:** a `paths` alias is now followed through a
  solution `tsconfig.json` (`references` plus `"files": []`, as in Vite's
  template) when the referenced configs that may compile the file agree. A
  stranded import of an inert module behind such an alias used to keep its
  bare import.
- **TypeScript and JavaScript:** an override left as `{}` is now dropped from
  the call it was passed to when it was the last argument and the function,
  declared at the top level of the same file and not generic, defaults that
  parameter to `{}`. `createWrapper(['/login'], {})` becomes
  `createWrapper(['/login'])`. The pull request lists each call it shortened.

### Fixed

- **TypeScript and JavaScript:** an override whose flag entry sat on a line of
  its own, leaving `{` and `}` on separate lines, was left empty without being
  listed in the pull request. It is now listed, or dropped as above.

## 1.13.0

### New

- **Every language:** a comment that names only the removed flag (or, in C#,
  only a type the run deleted) is removed, so the pull request no longer
  leaves `// Gated by the new-checkout flag.` behind for you to delete. A
  comment that also says anything else is kept and listed line by line, and
  the run's log reports it as a note rather than as a file that "still
  references" the flag. Licence headers, `TODO`s naming other work, doc
  comments as a whole, and text inside strings are never touched. See
  README's caveats for how "only" is decided.
- **Dart:** more unread private fields are deleted instead of being named in
  the pull request:
  - a field built by a constructor that only builds the object, a
    `FeatureClient();` or `FeatureClient() {}` with no initializer list in a
    class with no `extends` or `with` clause and no field initializer that
    may do work. That includes a keyless wrapper class
    (`final _flags = Flags();`) once its wrappers are deleted. Before, only a
    `const` constructor counted;
  - a field set by a constructor whose callers build the argument that way
    (`Svc(FeatureClient())`): the argument goes with the parameter;
  - a field that was the last use of an import: one of a library in the
    repository goes when nothing else it brings is used, and one of another
    package goes too when it has no prefix and one `show` list whose every
    name goes with the field
    (`import 'package:featureflip/featureflip.dart' show FeatureflipClient;`).
    A plain import of another package still keeps the field, since what it
    brings, extensions included, cannot be read.
- **Ruby:** a keyless wrapper is inlined at its callers and deleted once none
  is left, as in the other languages. It is a method of a class or module (an
  instance method, a `def self.` class method or a module function) whose
  whole body is one read of the flag
  (`def new_checkout? = @client.bool_variation("old-checkout", {}, false)`).
  A call is inlined only where its file proves it reaches that method: bare
  or through `self` inside the class's own methods, or through the class's
  name for a class method, with its value used and its arguments plain
  values. Because Ruby can define and call a method by name, a symbol or a
  string spelling the method anywhere keeps it, as do a test double, a class
  reopened in another file, `method_missing`, `prepend` and methods defined
  from computed names. A gem's instance methods are not inlined, and its
  public class methods are kept. Calls from ERB templates are not inlined
  yet; one keeps the wrapper. See README's "Wrapping the SDK".
- **Ruby:** Capistrano `.cap` tasks, Sorbet `.rbi` files and RBS `.rbs`
  signatures now count as Ruby files that can name a key constant or a
  wrapper, so a mention there keeps it.
- **Swift:** a keyless wrapper is inlined at its callers and deleted once none
  is left, as in the other languages. It is a function, or a method or
  computed property of a struct, enum or class, declared to return `Bool`,
  whose whole body is one read of the flag
  (`var newCheckout: Bool { client.boolVariation("old-checkout", default: false) }`).
  A call is inlined only where its file proves it reaches that declaration,
  in the wrapper's own module: a bare call of a function, the type's name or
  `Self` for a static member, and `self`, or a parameter, local or property
  written with the type as its type, for an instance member. A type with an
  inheritance clause, a subclassed or `open` class, and a member that is
  `open`, `class`, `override`, `dynamic` or has an attribute keep their
  wrappers, as do a key path, a `#selector` and a string naming the member. A
  `public` wrapper is kept unless its `Package.swift` declares no library
  product. See README's
  "Wrapping the SDK" for what is left alone, and why.

### Fixed

- **Dart:** a keyless wrapper called for nothing but its value, as the only
  statement of a braceless body (`if (c) flags.legacyCheckout;`, an `else`, a
  `for`, `while` or `do` loop), behind a label, or as the only statement of a
  `case`, is no longer deleted. Deleting it handed the body to the next
  statement, which then ran under the condition. The call is now kept and
  named in the pull request, and so is the wrapper.
- **Dart:** a private field whose type was named only in an import's `show`
  list is no longer deleted on the strength of that entry, which left the
  import unused (`unused_import`) when it came from another package.

## 1.12.0

### New

- **PHP:** a keyless wrapper is inlined at its callers and deleted once none
  is left, as in the other languages. It is a function, or a method of a
  class, declared to return `bool` (or with no return type), whose whole body
  is one read of the flag
  (`return $this->client->boolVariation('old-checkout', [], false);`). A call
  is inlined only where its file proves it reaches that declaration: a
  function name the file's namespace or a `use function` import resolves to
  it, the class's name or `self::` for a static method, and `$this`, a
  parameter typed as the class, or a `private` property typed as the class
  for an instance method. A class that is extended, abstract, has a parent,
  an interface, a trait or `__call`, or has a test double keeps its wrappers,
  as does a string naming the wrapper (a callable). A public wrapper of a
  Composer package that is not a `project` is kept. See README's "Wrapping
  the SDK" for what is left alone, and why.
- **Dart:** a private field left unread that a constructor sets is now
  deleted too, with what sets it: a `this._client` parameter, or an
  initializer-list entry (`: _client = client`) and the parameter it read
  when nothing else uses it. Every call of that constructor in the repository
  loses its argument, positional or named, through `new`, `const`, an import
  prefix, a redirecting `this(…)` or a subclass's `super(…)`. It happens only
  when every use of the class is accounted for: a private class in its own
  file, a public one in every Dart file of a package that is not published.
  A field whose initializer calls a `const` constructor with constant
  arguments (`final _client = FeatureClient();`) is deleted too. See README's
  Dart section for what is still kept and named in the pull request.

### Fixed

- **C#:** more gate families are removed whole instead of being kept by a
  proof that was too coarse:
  - a lookup over a local `Dictionary<K, Type>` of `typeof(…)` values no
    longer counts every string just because a file that does not parse uses
    a `Dictionary`; such a file may declare a type only under a name written
    right after `class`, `struct`, `interface`, `enum` or `record`;
  - in a file that compares names ignoring case, a string passed straight
    into another file's method (`Assert.True(Allowlist.IsAllowed(Kind.X,
    "IsEnabled"))`) no longer counts as naming the member;
  - a member lookup only tested for an attribute no longer keeps a gate
    whose wrapper file the run itself rewrote;
  - a constructor taking the gate interface is no longer kept by a
    target-typed `new(…)` whose type its context names
    (`static Resp Make() => new(…)`, `Resp r = new(…)`, an element of
    `new List<Resp> { new(…) }`) or that sits in a project that cannot see
    the class;
  - nor by `typeof` passed to your own attribute that keeps the `Type` in a
    get-only property only compared with `==` or `!=` (a test-coverage
    marker such as `[Covers(typeof(Job), …)]`).

## 1.11.0

### New

- **Python:** a function, method or `@property` whose whole body is one read
  of the flag (`return client.variation("new-checkout", ctx, False)`) is now
  inlined at every call its file proves is bound to it, and deleted once
  nothing names it. A function is proven when it is called bare in its own
  module, or through a top-level `from <its module> import name` or
  `import <its module> as m`; a method through `self` or `cls` inside its own
  class, or a static or class method through the class. The import of a
  deleted function is rewritten so the module still loads. A wrapper with a
  test double (`patch("app.flags.new_checkout")`, `MagicMock(new_checkout=...)`,
  `m.new_checkout.return_value = ...`) is left alone, and so is one exported
  through `__all__`, though its callers are still inlined. Set
  `inline-wrappers: false` to keep the old behaviour.
- **Dart:** a keyless wrapper is inlined at its callers and deleted once none
  is left, as in the other languages. It is a
  top-level function or getter, or a method or getter of a class, declared to
  return `bool`, whose whole body is one read of the flag
  (`bool get newCheckout => client.boolVariation('old-checkout', defaultValue: false);`).
  A call is inlined only where its file proves it reaches that declaration: a
  bare or prefixed call through an import of your package, the class's name
  for a static member, or a receiver every declaration of which in the file
  is written with the class as its type. A public wrapper of a package that
  may be published is kept. See README's "Wrapping the SDK" for what is left
  alone, and why.
- **Dart:** a private field whose every read was a flag read the run removed
  is deleted too, since `dart analyze` reports it as `unused_field` and exits
  non-zero. It goes when nothing else in its library sets it and its
  initializer cannot do work: absent, constant, or never run because the field
  is `late` or `static`. One that a constructor sets (`this._client` or an
  initializer list), one whose initializer may do work, one whose deletion
  would leave an import or a private declaration unused, an annotated field
  and a field of a library with `part` files are kept and named in the pull
  request instead.

### Behaviour changes

- **C#:** a gate method that builds its evaluation context before it reads
  the flag (`var ctx = new EvaluationContext { … }; if (orgId is { } o)
  ctx.Set(…); return _client.BoolVariation(…, ctx, false);`) is now inlined
  at its callers and removed like a one-line gate. Those statements may only
  build values that end up in the read's arguments.
- **C#:** a test fake set by a knob no longer has to default to the value the
  flag keeps serving, and may be a `private` class nested in a test class. A
  test that sets it to the removed value through a host's field
  (`factory.Gate.Enabled = false;`) is resolved like any other write, a test
  that hands it, never set, straight to what it builds is decided by its
  default, and a test that
  switches it to the removed value part-way through loses only the
  statements run that way. The pull request lists each such part.
- **C#:** once a gate family is complete, the run also removes a test host's
  field holding a fake (with the registration of it as the instance), a call
  of a test host's own registration helper, a middleware that only consulted
  the gate and now passes every request on (with its `UseMiddleware<…>()`
  registration and its tests), a test class the run's test edits left empty,
  and a comment standing alone above a deleted registration that names the
  flag or a deleted type.
- **C#:** a fallback's setting in a shared options class is removed even when
  tests build the options class into `new ConfigGate(Options.Create(new
  FeaturesOptions { … }))`, and when callers pass a configuration they build
  themselves or a top-level program's `builder.Configuration`.
- **C#:** a string such as `"*"` no longer keeps a gate as a possible prefix
  lookup unless your C# spells a lookup that takes a prefix (`GetMember`,
  `GetMembers`, `FindMembers`, `Type.FilterName`). Another language's source
  outside every C# project, such as a script's fixture, now counts as prose
  when it names a gate's type.

### Fixed

- A removal that deletes a whole file no longer fails the run after its
  branch is pushed. Switching the checkout back to your base branch was
  refused because the deleted files were back on disk but no longer tracked,
  which in `mode: remove` stopped every later flag in that run and in
  `mode: pr-command` reported a re-run that had updated the pull request as
  failed.

## 1.10.0

### Behaviour changes

- **TypeScript and JavaScript:** when a loop like `for (var i = 0; <flag>; i++)`
  is removed, the `var i = 0;` kept in its place is removed too if nothing
  else reads `i`. It used to be left behind.
- **TypeScript and JavaScript:** in a file that holds JSX, a named import the
  fold left unused is kept when another name in the same braces stays
  (`import { h, other } from "preact"`). With the classic JSX runtime every
  element calls its factory, such as Preact's `h`, without spelling it, so
  removing it broke the elements left behind. In a file that calls `eval`, no
  import is removed, since `eval` can read any of them by a string.
- **TypeScript and JavaScript:** in a file that holds JSX, the import of the
  JSX factory is kept even when it is the only name in its statement, such
  as `import React from "react"` or `import { h } from "preact"`. The factory
  is the `jsxFactory` or `jsxFragmentFactory` your `tsconfig.json` or
  `jsconfig.json` sets, or else `React` (and `h` and `Fragment` in a file that
  imports from `preact`), along with any name a `@jsx` or `@jsxFrag` comment
  gives. It used to be removed once the cleanup took out its last written
  use, which broke every JSX element left in the file.

## 1.9.0

### Behaviour changes

- **Java:** a local holding a literal that the fold left unread is removed
  inside a lambda, an anonymous class or a `static` initializer too. It used to
  be left there.
- **Kotlin:** a local holding a literal that the fold left unread is removed
  inside a lambda, an anonymous function, an object expression, an `init`
  block or a property accessor too. It used to be left there. A lambda's last
  statement still stays, since it is the lambda's value.
- **Kotlin:** an aliased import the fold left unused (`import a.b.C as D`) is
  removed when the file spells neither `D` nor `C`. It used to be kept in every
  case.
- **Java and Kotlin:** removing an import that sat alone between two blank
  lines also removes one of them, so the file is not left with two blank lines
  in a row.

## 1.8.0

### Behaviour changes

- **Python:** a local the fold left unread is kept, not deleted, when deleting
  it could change or break the function. That covers a function that spells any
  non-ASCII name (Python reads `ｒetries` as `retries`), a dunder such as
  `__class__`, a `~` on a number (`~1.5` raises), a container inside a set or
  used as a dict key (`{[1]}` raises), and a first statement followed by a
  parenthesised string (`("doc")` would become the docstring).
- **C#, Dart, Go, Java, JavaScript, Kotlin, PHP, Ruby, Swift and TypeScript:**
  a comment at the end of a removed statement's line is removed with it. It
  used to be left at the end of the line above (`{ // remove after rollout`)
  or after the member that stays, where it read as a note about kept code.
- **C#, Dart, Go, Java, JavaScript, Kotlin, PHP, Ruby, Swift and TypeScript:**
  code that followed a removed statement on its line keeps a line of its own.
  It used to be joined onto the line above, and could end up inside a comment
  there. In Kotlin and Swift, a removed statement followed on its line by a
  block comment and more code (`dead(); /* note */ next()`) is refused
  instead, because the line left behind does not parse.
- **Python:** removing the last `elif`, `else`, `except` or `finally` clause
  keeps the blank lines after it, so the two blank lines before the next
  top-level function stay. The same holds for a `finally` on its own line
  after a `catch` in TypeScript and JavaScript.
- **Python:** a literal local the fold left unread is removed even when a
  comment on its line mentions its name (`retries = 3  # retries left`).
- **Ruby:** more locals the fold left unread are removed: one written in a
  block inside a method, an only `x ||= <literal>` or `x &&= <literal>`, and a
  method's or block's last `x = value`, which becomes `value`, so what it
  returns is unchanged. Removing a last `elsif` or `else` also removes a blank
  line directly above it, which would have sat just before `end`.
- **Ruby:** a local is kept when deleting it could change or break the method:
  a `~` on anything but an integer (`~1.5` raises), a range whose ends do not
  compare (`1..'a'` raises), a range end under `!`, `not` or `defined?`
  (`!1..2` raises), and a local
  next to a range left open at the end of a line (`x = 1..`), which Ruby
  continues onto the next line.
- **PHP:** a local is kept when its function names `assert`,
  `mb_ereg_replace` or `mb_eregi_replace`, which on PHP 7 can run a string
  that reads it as code. An `Assert::` class counts too.

## 1.7.0

### Removes more

- **C#:** what a complete keyless wrapper with an interface leaves behind is
  removed too: the emptied interface and classes (and their files), their
  dependency-injection registrations, the constructor parameters, fields and
  arguments that passed the interface around, the test mocks and tests that
  only built them, the options class (or setting) an inert implementation read
  with its binding and `appsettings*.json` key, and the `using`s of a namespace
  this empties. Anything that cannot be proven keeps all of it, and the pull
  request says why; the flag is removed either way.
- **Go:** a label whose last `break`, `continue` or `goto` the fold removed is
  removed too. Go rejects an unused label, so the pull request did not build.
- **TypeScript and JavaScript:** an unread `private` or `#` field is removed,
  not just named in the pull request, when deleting it skips no work. The
  constructor parameter that fed it, and every caller's argument, go too when
  that can be proved.
- **TypeScript and JavaScript:** an unread constructor parameter is removed
  through a subclass that has no constructor of its own. When the removal
  leaves a subclass's plain constructor parameter unused
  (`constructor(c, l) { super(c, l); }`), that parameter and every caller's
  argument are removed too. A case that cannot be proved is named in the pull
  request, for `noUnusedParameters`.
- **C#:** removing an unread primary-constructor parameter now continues down
  a forwarding chain (`class Passing(IClient c, ILog l) : Svc(c, l)`), so no
  CS9113 is left behind. Callers that pass named arguments
  (`new Svc(client: c, log: l)`, `: base(…)`, `: this(…)`) no longer block the
  removal.
- **JavaScript:** a keyless wrapper with its own hand-written `.d.ts` is
  deleted together with its declarations, instead of being kept.
- **Java and Kotlin:** a keyless wrapper behind a public interface is inlined
  when its Gradle or Maven module provably publishes nothing. Anything the
  Action does not recognise counts as published, and the interface stays.
- **TypeScript:** a jest-mock-extended or ts-mockito class mock declared again
  in every test (`const m = mock<Flags>()`) no longer keeps its wrapper from
  being removed. The `let m; beforeEach(…)` shape still does.
- **TypeScript and JavaScript:** a stranded import of a Featureflip SDK
  (`@featureflip/js`, `/node`, `/browser`, `/react`, `/react/testing`) is
  deleted, instead of being left as a bare `import '@featureflip/react';`.
  This includes a `tsconfig.json` or `jsconfig.json` that extends a base config
  by relative path, which the Action reads the way the compiler does. The
  import is kept when the compiler could read the name as a file in your
  repository, or when a config cannot be read for certain. That covers a
  package `extends` such as a `@tsconfig/*` preset, a `baseUrl` that starts
  with `${configDir}` or is `null`, `paths` set to `null`, `moduleSuffixes`, a
  repeated key, and a file such as `@featureflip/react.ts` in the importing
  file's directory or one above it.
- **TypeScript and JavaScript:** a stranded CommonJS `require` of a module that
  does nothing on load is deleted (`import x = require(…)`,
  `const client = require(…)` and the destructured form), instead of being
  left as `require("./m");`.
- **TypeScript and JavaScript:** a `do { … } while (false)` left by a fold is
  unwrapped, and an emptied `static {}` block is deleted.
- **Kotlin:** dead `when` arms that share a line with another arm
  (`a -> 1; false -> 2`) fold. They were refused before.
- **C#:** a key constant read through its type from the same file
  (`Flags.NewCheckout` beside `public static class Flags`) is folded and its
  declaration removed. The run used to report no changes.
- **C#:** an interpolated string with an escaped brace right before a hole
  (`$"{{{Flags.On("k")}}}"`) is now analysed, so a flag read inside the hole
  folds. The file used to be refused. A key constant read only inside such a
  hole is now seen, so its declaration is kept.
- **Java:** the statement after a `try` whose body the fold left always
  exiting is removed and listed, unless a `catch` block can fall through, and
  so is a statement after a folded `yield`. Both are `unreachable statement`
  errors, and the pull request did not build.
- **Go:** two names declared together and both left unused by the fold
  (`x, y := 3, load()`, `var low, high = 1, 9`) are removed, the value kept as
  `_ = load()` when it may have a side effect. So is a local whose name a
  function literal's parameter, a same-named local in another block or a
  struct literal key also spells. These flags used to be refused.
- **Kotlin and Dart:** a short string template that reads an inlined local
  (`"$ok yes"`, `'$enabled'`) is rewritten as the braced literal,
  `"${true} yes"`. The flag used to be refused.
- **PHP:** an `if` without braces folds (`if (!<read>) return 1;`), and so does
  a `match`: an arm of `match (true)` with the read as its condition, a
  `match` left with only its `default`, and a `match` on the read itself.
  These used to be refused.
- **Dart:** a collection-`for` whose element is a collection-`if` on the read
  folds. Served false, the whole `for` goes when it loops over a `List` or
  `Set` parameter inside a literal whose element type is written out;
  anywhere else the flag is still refused.

### Refuses instead of rewriting

Each of these used to be rewritten in a way that could change what the code
does. The run now exits 1 and names the file. Fix it by hand, or add the key
to `ignore`.

- **Java and C#:** a pattern guard served true (`case String s when <read>`)
  ahead of any later case but `default` / `_`, once a later case with the very
  same pattern has been deleted (it could never run). Both compilers read
  `when true` as no guard, so a later case the pattern covers no longer
  compiled. In Java, a colon-form `case … when <read>:` is refused on both
  values, since `javac` rejects a constant guard there.
- **Go:** a local bound to the read is refused when the function reads an
  outer variable of the same name above the local's declaration. The inlining
  rewrote that read too. When the function spells the name for anything else
  (a `range` variable, an `if` initializer, a function literal's parameter, a
  later `:=`, an assignment, a struct literal key or `&name`), the local is not
  inlined and `name := true` stays with its reads. The inlining used to rewrite
  those too: most results did not build, and a `range` variable became
  `for _, true := range …`, which builds and runs the loop differently.
- **Go:** a flag whose removal leaves a local declared and no longer used,
  when neither cleanup can remove it: two names declared together when one is
  still read, three or more names, a `:=` that also redeclares a name, several
  names on one spec in a `var ( … )` group, or a local whose name is also a
  struct literal key of a type not declared in the file. The pull request used
  to open anyway, and Go rejected it with `declared and not used`. The output
  names the local and its function.
- **PHP:** a dangling `else` (`if ($a) if (<read>) { … } else { … }`, also
  behind a brace-less `foreach`, `for` or `while`). PHP gives the `else` to the
  inner `if`, and the fold used to hand it to the outer one, on both values.
- **PHP:** a `match` on the read whose arm lists a call before the literal
  (`audit(), true => …`). The fold used to pick the arm and drop the call.
- **TypeScript and JavaScript:** a dead branch that declares a `var`. The name
  is hoisted and may be read after the `if`, so the branch is kept, and the
  `if` left testing a literal is refused. Deleting the branch used to drop the
  declaration.
- **Kotlin:** an `if` used as an expression whose surviving branch holds more
  than one statement. Splicing it flat made the branch's first statement the
  value.
- **C# and TypeScript constructor-parameter removal** is refused when the
  class is also constructed in a way the Action cannot follow: a C# attribute
  (`[Foo(…)]`), an XML-doc `cref`, `Activator.CreateInstance` with the type
  named in a string, or a TypeScript computed construction
  (`new m['Svc'](…)`, `Reflect.construct`).
- **C#** constructor-parameter removal is refused when the class has another
  explicit constructor with parameters (which used to produce a duplicate
  constructor, CS0111), and for a `partial` derived type.
- **TypeScript and JavaScript** constructor-parameter removal is refused when
  an `.html` page mentions the class, because a `<script>` there may call it.
- **TypeScript and JavaScript:** a `do { … } while (false)` that cannot be
  unwrapped safely is refused. A comment outside its braces used to be dropped.
- **Ruby:** an `if` or `unless` used as a value folds only when the branch it
  keeps is one simple expression. Several statements, a call whose arguments
  have no parentheses, and a dead branch in an argument are refused. So is a
  subject-less `case` in the same position, a `when a, b`, and the only `when`
  of a `case` that keeps an `else`. `x = if <read>; a; b end` used to become
  `x = a` followed by a loose `b`.

### Behaviour changes

- **Every language but TypeScript, JavaScript and Python:** a call to the left
  of the read is kept. `track() || <read>` served true and `track() && <read>`
  served false used to fold to the bare literal, dropping the call and what it
  did; they now come out as `track() || true` and `track()`.
- **TypeScript and JavaScript:** a conditional callee folded to one side keeps
  the `this` the call had. `(<read> ? obj.m : other)()` becomes `(0, obj.m)()`
  or `other()`, never `obj.m()` (which binds `obj`) or `(other)()` (which
  TypeScript's CommonJS output binds to the module object).
- **Every language:** blank lines inside a multi-line string literal (a
  template literal, a raw or verbatim string, a text block, a heredoc) are left
  alone. Every rewritten file used to have them collapsed, changing the value.
- **Go:** a read bound beside another name (`a, on := load(), <read>`) keeps the
  other binding, `a := load()`. Both names used to be deleted, taking the call
  with them. What follows a folded `break`, `continue` or `goto`, a `return` in
  a `case` or `select` clause, or a `switch` whose clauses all return, is
  deleted, so `go vet` reports no unreachable code. A comment after the `}` the
  fold removes trails the spliced body instead of landing on a line of its own.
- **Ruby:** a local written more than once, read past the branch or block that
  writes it, or an instance variable keeps its declaration as `x = true`.
  Inlining it used to leave `nil`, a `NameError` or `true ||= x`. A write used
  as a value (`if (x = <read>)`, `x = <read> or y`) is replaced by its value
  rather than deleted, and a dead modifier `x if <read>` that was a method's
  last statement becomes `nil`, keeping what the method returns.
- **Ruby:** a local that nothing in its method reads is also cleaned up when
  the removed branch spelled its name as a method or a hash key (`retries = 3`
  beside a dead `g(retries: 1)` or `obj.retries`): a literal is deleted and a
  call kept as a statement, as for a local the fold stranded. It was already
  unused, and RuboCop flags it; the pull request names it with the rest.
- **Swift:** a `var` bound to the read that is later assigned, has a method
  called on it (`x.toggle()`) or is passed `inout` keeps its declaration.
  Inlining it used to write `true = …` or `true.toggle()`.
- **C#:** a local passed as an `out` or `ref` argument, or written by a
  deconstruction, keeps its declaration (`out true` did not compile). A local
  function declared after a folded `return` is kept, since the code that runs
  may call it.
- **Java and C#:** a later `switch` case with the same pattern as a guard
  served true is deleted. It could never run, and the flag used to be refused.
- **Kotlin:** a private property read as `this.name` folds (`this.true` used
  to be written). A surviving branch that is an unbraced loop body keeps its
  braces.
- **Dart:** a redundant `x = true;` that is an unbraced `if` body becomes `{ }`.
  Deleting it handed the body slot to the next statement.
- **PHP:** where a logical operator folds away in a value position, the operand
  it keeps is cast (`return (bool) $x;`), since `&&`, `||`, `and` and `or`
  always yield a bool.
- **PHP:** a `match` folded inside a larger expression keeps its parentheses:
  `match (true) { <read> => $a + 1, default => 0 } * 3` becomes
  `($a + 1) * 3`, never `$a + 1 * 3`. A `goto` label after a folded `return`
  is kept.
- **Go:** a comment at the end of a removed local's line goes with the local.
  Left behind, it ended up on the function's signature line.
- **Kotlin, Java and Go:** a function with the same name and parameters as the
  one reading the flag, in another class of the same file, is left alone. The
  inlining used to rewrite it as well, which in Kotlin could write
  `val true = …`.
- **Kotlin and Dart:** a local is inlined only when every read of its name lies
  between its declaration and the end of its block. A Kotlin `private`
  property read bare inside a lambda, an extension, an object expression or a
  nested class, or shadowed by a constructor parameter, keeps its declaration.
- **TypeScript and JavaScript:** a comment between `else` and its unbraced
  statement no longer swallows that statement, and an arrow body that folds to
  an object literal is parenthesised (`=> ({ … })`) rather than becoming a
  block.
- **ERB:** a deleted tag written with trim markers takes the newline and
  indentation they trimmed, so a `.text.erb` template renders exactly as before.
- **Every language:** a flag key is matched only against a string literal's
  whole value. A literal that merely starts or ends with the key, because an
  interpolation or an escape splits it (Python `f"old-checkout{env}"`, PHP
  `"old-checkout$env"`, Swift `"old-checkout\(env)"`, `"old-checkout\x2dv2"`),
  reads a different flag, and that read used to be folded and its key constant
  deleted. It is now left alone.
- **Archive modes:** only a removal merged into the base branch counts. A
  removal pull request retargeted to another branch and merged there used to
  archive the flag although the base branch still read it. The base branch is
  `base-branch`, or the repository's default branch when that is unset, so a
  cleanup workflow that sets `base-branch` needs the same value in its
  `archive-sweep` and `archive-on-deploy` workflows.
- **`archive-on-merge`:** a pull request from a fork never archives anything,
  whatever its branch is called. Under `pull_request_target`, a fork branch
  named like a removal branch could archive any flag once merged.
- **Archive modes and `remove`:** a GitHub answer to a pull-request lookup that
  is not a list (a proxy's HTML page, an error object sent with status 200) now
  fails the run instead of reading as "no pull request". Before, the sweep
  could drop a merged removal from its backlog, and `remove` could treat a
  branch as having no pull request.
- **C#:** a stranded `private` method is kept when deleting it would leave a
  field that only it assigned (CS0649) or only it read (CS0169, CS0414), each
  an error under `TreatWarningsAsErrors`.
- **C#:** a key constant declared in another file is kept, and the pull
  request says why, when a file reflects over its class as a whole
  (`typeof(FeatureKeys).GetFields()`, `Type.GetType("Shop.Flags.FeatureKeys")`,
  a subclass or an alias of it). Before, the constant was deleted once every
  read that named it was folded, and the reflection silently lost it.
- **C#:** the same holds when the reflection is in a Razor view, a XAML file,
  or a VB, F# or other .NET file the Action does not rewrite
  (`typeof(FeatureKeys)`, `GetType(FeatureKeys)`, `typeof<FeatureKeys>`,
  `{x:Type local:FeatureKeys}`). Reading another constant there
  (`FeatureKeys.Other`) still lets the removed one go.
- **Dart:** a key constant declared in another file is kept when any file
  imports or exports `dart:mirrors`. A mirror reaches a constant without
  naming it (`reflectClass(FeatureKeys).declarations`, or every loaded library
  through `currentMirrorSystem()`), so the constant used to be deleted while
  still reachable. Mirrors exist only on the Dart VM, so Flutter and AOT
  packages are unaffected.
- **Java and Kotlin:** a key constant declared in another file is kept, and
  the pull request says why, when any Java, Kotlin, Groovy or Scala file takes
  its owner whole: a class literal (`RuntimeKeys.class.getFields()`,
  `RuntimeKeys::class.java`), `Class.forName("app.RuntimeKeys")`, the same for
  a subclass, or a Kotlin object handed on as a value. A class handed to a
  test double or a logger (`mock(CheckoutGate.class)`, Spock's
  `Mock(CheckoutGate)`, `mockk`, `getLogger`) or named in Spring's `@Import`
  or a Spring test double's annotation (`@MockBean`, `@MockkBean`) does not
  count. Before, the
  constant was deleted once every read that named it was folded.
- **PHP:** a key constant declared in another file is kept when a heredoc, a
  nowdoc, a `b'...'` string or a string spelling the class through escapes
  (`"App\\Flags\\\x4Beys"`) names its class. Those strings were not read
  before, so the constant was deleted while `constant()` or reflection could
  still reach it.
- **PHP:** a namespace-level key constant (`const NEW_CHECKOUT = '...';`) is
  kept when its name can be built at run time: `constant('App\\Flags\\' . $n)`,
  `constant(__NAMESPACE__ . '\\' . $n)` inside that namespace, or
  `get_defined_constants()`, which lists every user constant. These used to
  let it be deleted.
- **PHP:** a key constant declared in another file is kept, and the pull
  request names the line, when a Twig, Latte, Smarty, Volt or Blade template
  builds a constant's name from its class
  (`constant('App\\Flags\\Keys::' ~ name)`,
  `constant(\App\Flags\Keys::class . '::' . $name)`). Before, the constant
  was deleted and the template's lookup failed at run time. A template that
  reads another constant by its whole name (`constant('App\\Flags\\Keys::ALL')`)
  does not keep the key constant.
- **PHP:** a class key constant is also kept when an instance of its class
  is created (`new Keys()`), received through a typed parameter, property or
  return (`Keys $k`, which a container autowires), or handed on as `$this`
  from a subclass. An instance reaches every constant
  (`new \ReflectionObject($k)`, `get_class($k)`), and where it goes is not
  followed. A service that declares its own flag constants and is constructed
  or injected now keeps them.
- **Ruby:** a key constant is kept when another file holding Ruby reads it,
  not only a `.rb`, `.rake` or `.erb` file. That covers views (`.jbuilder`,
  `.jb`, `.haml`, `.slim`, …), project files (`Rakefile`, `Gemfile`,
  `config.ru`, a `.gemspec`, …) and `bin/` scripts with a Ruby shebang.
  `Gate::FLAG_KEY` in a HAML view used to let the constant be deleted. HAML and
  Slim are not parsed, so one keeps the constant whenever it names it or its
  owner.
- **Ruby:** a flag read in one of those files is now listed in the pull
  request among the files that still reference the flag. The Action does not
  rewrite them, and it used to say nothing about them, so the pull request
  looked like a complete removal.
- **PHP, Java, Kotlin, C#, Python, Ruby and Dart:** a key constant is kept,
  and the pull request names the line, when the file declaring it takes its
  owner whole: `(new \ReflectionClass(self::class))->getConstants()` inside
  the class, `Keys.class.getFields()` inside `Keys`, `typeof(FeatureKeys)`
  inside `FeatureKeys`, `constants` or `const_get` in a Ruby module, a Python
  module importing and handing on itself, or `dart:mirrors` imported there.
  Before, only another file counted, so the constant was deleted and the
  owner's own listing silently lost it. The file is left as it is.
- **C# and Java:** naming a class for a logger or an equality contract no
  longer keeps its key constants, in any file: `ILogger<Owner>`,
  `Log.ForContext<Owner>()`, `LogManager.GetLogger(typeof(Owner))`,
  `IEquatable<Owner>`, `IComparable<Owner>`, and
  `Logger.getLogger(Owner.class.getName())`. None of them reads a field.
- **Ruby:** a dead `if`, `unless`, `case` or `while` that was the last
  statement of a method or block becomes `nil`, so the method returns what it
  did before. A dead loop there is listed in the pull request.
- **Ruby:** a callback condition you wrote yourself (`if: -> { true }`) and an
  empty RSpec hook (`before(:each) { }`) are left alone. Only ones a flag
  removal produced are cleaned up.
- **Java and Dart:** every statement a fold left unreachable after a `return`
  is listed in the pull request. The first one used to be missing.
- **Dart:** a local the fold left unused that held a number, a string, a
  boolean, `null`, a list, set or map of those, or no value at all, goes with
  the comment at the end of its line. The comment used to be left behind on a
  line of its own.
- **Every language but Go and ERB:** a blank line a removal leaves just inside
  a class, method or block, after its opening line or before its closing `}`
  or `end`, is removed. RuboCop's `Layout/EmptyLinesAround*Body` cops used to
  flag it. A blank line that was already there is kept.
- **Every language:** a file with Windows (CRLF) line endings throughout keeps
  exactly one `\r\n` per line wherever the removal deletes or folds code.

### Output

- **Go:** a comment on a removed import's line goes with it, and so does the
  comment heading a section the import was alone in. Left behind, either one
  made `gofmt -l` list the file.
- A `//` comment that trailed a removed argument or parameter goes with it,
  instead of moving onto the item before. A tab between items stays a tab.
- The text a fold moves is re-indented to where it lands, and the blank line a
  removed `}` or `end` left behind is gone (Go, Ruby, Swift).
- Deleting a file's first statement takes the line break after it, so the file
  no longer starts with a blank line.
- **ERB:** a multi-line tag the removal rewrote comes back as one tag. Split in
  two, it rendered an extra newline.

### Integrations

- **CircleCI:** the `featureflip/flag-cleanup` orb is published. The README has
  a "Running it on CircleCI" section. The orb offers `remove` and
  `archive-sweep`.
