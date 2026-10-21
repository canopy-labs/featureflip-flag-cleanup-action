# Changelog

What changes for you in each release. The newest is first.

A release takes its section from this file: the section for the version being
released becomes the body of its GitHub Release, and a release whose version
has no section here is refused before anything is built. Changes that have not
been released yet collect under **Unreleased**.

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
