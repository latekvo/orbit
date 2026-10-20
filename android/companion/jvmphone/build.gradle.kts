plugins {
    alias(libs.plugins.kotlin.jvm)
    application
}

kotlin {
    jvmToolchain(17)
}

dependencies {
    implementation(project(":protocol"))
}

application {
    mainClass.set("dev.orbit.jvmphone.MainKt")
}
