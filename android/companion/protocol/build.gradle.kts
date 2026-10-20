plugins {
    alias(libs.plugins.kotlin.jvm)
}

kotlin {
    jvmToolchain(17)
}

dependencies {
    api(libs.kotlinx.serialization.json)
    testImplementation(libs.kotlin.test)
    testImplementation(libs.junit)
}

tasks.test {
    // spec/test-vectors.json lives at the repository root.
    systemProperty("orbit.vectors", rootProject.file("../../spec/test-vectors.json").absolutePath)
    testLogging { events("passed", "failed", "skipped"); exceptionFormat = org.gradle.api.tasks.testing.logging.TestExceptionFormat.FULL }
}

// Connectivity harness: the real ResponderSession + Handshake behind a JVM TCP server (like the
// app's TcpServer, with the app's real admission limits), driven by desktop/tests/connectivity.
val harness: SourceSet = sourceSets.create("harness") {
    kotlin.srcDir("src/harness/kotlin")
    kotlin.srcDir("../app/src/main/java")
    kotlin.include("dev/orbit/harness/**", "dev/orbit/companion/UnauthenticatedSessions.kt")
    compileClasspath += sourceSets.main.get().output
    runtimeClasspath += sourceSets.main.get().output
}
configurations[harness.implementationConfigurationName].extendsFrom(configurations.api.get())

tasks.register("harnessClasspath") {
    description = "Builds the connectivity harness and writes its runtime classpath to build/harness.classpath."
    dependsOn(harness.classesTaskName)
    val cp = harness.runtimeClasspath
    val out = layout.buildDirectory.file("harness.classpath")
    outputs.file(out)
    outputs.upToDateWhen { false }
    doLast { out.get().asFile.writeText(cp.asPath) }
}
