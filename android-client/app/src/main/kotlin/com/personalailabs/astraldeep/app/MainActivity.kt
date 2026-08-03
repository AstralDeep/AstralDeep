package com.personalailabs.astraldeep.app

import android.app.DownloadManager
import android.content.Context
import android.net.Uri
import android.os.Bundle
import android.os.Environment
import android.util.Log
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.viewmodel.compose.viewModel
import com.personalailabs.astraldeep.app.auth.ConversationResumeStore
import com.personalailabs.astraldeep.app.auth.ConversationResumeStore.ClearReason
import com.personalailabs.astraldeep.app.auth.KeycloakLogout
import com.personalailabs.astraldeep.app.auth.OidcAuth
import com.personalailabs.astraldeep.app.auth.TokenStore
import com.personalailabs.astraldeep.app.auth.keycloakEndpoints
import com.personalailabs.astraldeep.app.auth.routeAfterRefresh
import com.personalailabs.astraldeep.app.render.Download
import com.personalailabs.astraldeep.app.render.Emit
import com.personalailabs.astraldeep.app.render.Renderer
import com.personalailabs.astraldeep.app.render.ThemeSink
import com.personalailabs.astraldeep.app.render.renderers.registerAllRenderers
import com.personalailabs.astraldeep.app.rest.AstralRest
import com.personalailabs.astraldeep.app.transport.ConnectionState
import com.personalailabs.astraldeep.app.transport.OrchestratorClient
import com.personalailabs.astraldeep.app.transport.deviceCapabilities
import com.personalailabs.astraldeep.app.transport.runtimeVoiceCapability
import com.personalailabs.astraldeep.app.transport.voiceDeviceId
import com.personalailabs.astraldeep.app.ui.AppViewModel
import com.personalailabs.astraldeep.app.ui.RootScaffold
import com.personalailabs.astraldeep.app.ui.theme.AstralColors
import com.personalailabs.astraldeep.app.ui.theme.AstralTheme
import com.personalailabs.astraldeep.app.voice.LiveKitVoiceMediaClient
import com.personalailabs.astraldeep.app.voice.OkHttpVoiceControlApi
import com.personalailabs.astraldeep.app.voice.VoiceSessionController
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class MainActivity : ComponentActivity() {
    private val client by lazy { OrchestratorClient(AppConfig.WS_URL) }
    private val rest by lazy { AstralRest(AppConfig.API_BASE) }
    private val voiceScope by lazy { CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate) }
    private val voiceController by lazy {
        VoiceSessionController(
            api = OkHttpVoiceControlApi(AppConfig.API_BASE),
            media = LiveKitVoiceMediaClient(this, voiceScope),
            scope = voiceScope,
        )
    }
    private val oidc by lazy { OidcAuth(this) }
    private val keycloakLogout by lazy {
        KeycloakLogout(keycloakEndpoints(AppConfig.KEYCLOAK_AUTHORITY).endSessionEndpoint)
    }

    /** Download an authed backend file (`/api/download/...`) to the device's public
     * Downloads via the system DownloadManager, forwarding the session bearer token. */
    private fun downloadFile(
        url: String,
        filename: String,
    ) {
        try {
            val full = if (url.startsWith("http")) url else AppConfig.API_BASE.trimEnd('/') + url
            val req =
                DownloadManager.Request(Uri.parse(full))
                    .addRequestHeader("Authorization", "Bearer ${authToken.value.orEmpty()}")
                    .setTitle(filename)
                    .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                    .setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, filename)
            (getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager).enqueue(req)
            Toast.makeText(this, "Downloading $filename…", Toast.LENGTH_SHORT).show()
        } catch (e: Exception) {
            Log.w("MainActivity", "download failed: ${e.message}")
            Toast.makeText(this, "Download failed", Toast.LENGTH_SHORT).show()
        }
    }

    private val store by lazy { TokenStore(this) }
    private val conversationResumeStore by lazy { ConversationResumeStore(this) }
    private val authToken = MutableStateFlow<String?>(null)
    private val signInError = MutableStateFlow<String?>(null)

    private val authLauncher =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
            val data = result.data ?: return@registerForActivityResult
            lifecycleScope.launch(Dispatchers.IO) {
                runCatching {
                    val state = oidc.exchange(data)
                    val token = oidc.freshToken(state)
                    store.save(state) // persist AFTER the first refresh (captures rotation)
                    token
                }.onSuccess {
                    authToken.value = it
                    signInError.value = null
                }.onFailure {
                    Log.w("MainActivity", "sign-in exchange failed: ${it.message}")
                    signInError.value = it.message ?: "sign-in failed"
                }
            }
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Resume a cached session. Per the sign-in-once-a-year policy: if credentials
        // are found on the device, go straight to the home screen — show it right away
        // with the cached access token, then refresh silently and PERSIST the (rotated)
        // refresh token so the session survives future cold starts. A DEFINITIVE
        // refresh rejection routes to the sign-in screen with an explanation, never a
        // dead app (T016); a transient failure (offline, IdP briefly down) keeps the
        // cached token so a valid year-long session is never kicked out offline.
        lifecycleScope.launch(Dispatchers.IO) {
            val st = store.load() ?: return@launch
            val cached = st.accessToken?.takeIf { it.isNotBlank() }
            cached?.let { authToken.value = it }
            val route =
                routeAfterRefresh(
                    runCatching { oidc.freshToken(st) }
                        .onSuccess { store.save(st) }
                        .onFailure { Log.w("MainActivity", "silent token refresh failed: ${it.message}") },
                    cachedToken = cached,
                )
            authToken.value = route.token
            signInError.value = route.error
        }
        setContent {
            val vm: AppViewModel =
                viewModel(factory = AppViewModel.factory(client, rest, conversationResumeStore, voiceController))
            // Collect once at the top so the theme can restyle live (US5): the palette
            // drives AstralTheme, and recomposition repaints the whole tree.
            val uiState by vm.state.collectAsStateWithLifecycle()
            AstralTheme(palette = uiState.themePalette) {
                val renderer =
                    remember(vm) {
                        Renderer(
                            Emit { a, p -> vm.sendEvent(a, p) },
                            Download { url, fn -> downloadFile(url, fn) },
                            ThemeSink { spec -> vm.applyTheme(spec) },
                        ).registerAllRenderers()
                    }
                val token by authToken.collectAsStateWithLifecycle()
                val error by signInError.collectAsStateWithLifecycle()

                if (token == null) {
                    SignInScreen(error = error, onSignIn = ::startSignIn)
                } else {
                    LaunchedEffect(token) {
                        val dm = resources.displayMetrics
                        vm.start(
                            token = token!!,
                            device =
                                deviceCapabilities(
                                    widthPx = dm.widthPixels,
                                    heightPx = dm.heightPixels,
                                    pixelRatio = dm.density.toDouble(),
                                    supportedTypes = renderer.supportedTypes.toList(),
                                    deviceId = voiceDeviceId(this@MainActivity),
                                    voice = runtimeVoiceCapability(this@MainActivity),
                                ),
                        )
                    }
                    // Mid-session token expiry: silently refresh and reconnect; if the
                    // refresh fails, drop to the sign-in screen WITH an explanation
                    // (FR-012/T016) — never a silent dead session.
                    LaunchedEffect(uiState.connection) {
                        if (uiState.connection == ConnectionState.AuthRequired) {
                            val route =
                                withContext(Dispatchers.IO) {
                                    routeAfterRefresh(
                                        runCatching {
                                            val st = checkNotNull(store.load()) { "no stored session" }
                                            oidc.freshToken(st).also { store.save(st) }
                                        },
                                    )
                                }
                            authToken.value = route.token
                            signInError.value = route.error
                        }
                    }
                    RootScaffold(vm, renderer, onSignOut = ::signOut)
                }
            }
        }
    }

    private fun startSignIn() {
        runCatching { authLauncher.launch(oidc.authorizeIntent()) }
            .onFailure { signInError.value = it.message ?: "could not start sign-in" }
    }

    override fun onStart() {
        super.onStart()
        voiceController.appForegroundChanged(active = true)
    }

    override fun onStop() {
        voiceController.appForegroundChanged(active = false, reason = "backgrounded")
        super.onStop()
    }

    /**
     * Sign out (T019): capture the session's tokens, clear local state immediately
     * (the sign-in screen never waits on the network), then best-effort server-side
     * revocation — the backend `/api/auth/logout` first, direct Keycloak logout as
     * the fallback — so the refresh token dies even when the backend is down.
     */
    private fun signOut() {
        voiceController.logout()
        // Clear the LOCAL session SYNCHRONOUSLY on the main thread first, so
        // sign-out is durable even if the Activity is destroyed an instant later.
        // (Doing the clear inside a cancellable lifecycleScope coroutine risked
        // cancellation before store.clear() ran → the user silently still signed
        // in on the next cold start.) Capture the refresh token BEFORE clearing.
        val st = runCatching { store.load() }.getOrNull()
        val access = authToken.value ?: st?.accessToken
        val refresh = st?.refreshToken
        access
            ?.let(ConversationResumeStore::accountFromAccessToken)
            ?.let { account ->
                if (!conversationResumeStore.clear(account, ClearReason.DEFINITIVE_SIGN_OUT)) {
                    Log.w("MainActivity", "conversation locator clear failed during sign-out")
                }
            }
        store.clear()
        signInError.value = null
        authToken.value = null
        if (refresh.isNullOrBlank()) return
        // Best-effort server-side revocation off the main thread — fine to be
        // cancelled at onDestroy, the local session is already gone.
        lifecycleScope.launch(Dispatchers.IO) {
            val viaBackend =
                access != null &&
                    runCatching { rest.logout(access, refresh, AppConfig.OIDC_CLIENT_ID) }.getOrDefault(false)
            val outcome =
                if (viaBackend) {
                    "backend"
                } else if (runCatching { keycloakLogout.revoke(AppConfig.OIDC_CLIENT_ID, refresh) }.getOrDefault(false)) {
                    "keycloak"
                } else {
                    "unrevoked"
                }
            Log.i("MainActivity", "sign-out revocation: $outcome")
        }
    }

    override fun onDestroy() {
        oidc.dispose()
        super.onDestroy()
    }
}

/**
 * The sign-in landing. Painted on its own [Surface] so content color resolves to
 * the theme's on-background (the earlier bare Column rendered text in the default
 * black, which was invisible on the dark backdrop). The AstralDeep wordmark is the
 * hero; a single gradient "Sign in" launches the Keycloak PKCE flow.
 */
@Composable
private fun SignInScreen(
    error: String?,
    onSignIn: () -> Unit,
) {
    Surface(modifier = Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Box(
            modifier = Modifier.fillMaxSize().background(AstralColors.BackdropBrush).padding(28.dp),
            contentAlignment = Alignment.Center,
        ) {
            Column(
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.Center,
                modifier = Modifier.widthIn(max = 420.dp).fillMaxWidth(),
            ) {
                Image(
                    painter = painterResource(R.drawable.astral_logo),
                    contentDescription = "AstralDeep",
                    contentScale = ContentScale.Fit,
                    modifier = Modifier.fillMaxWidth(0.78f).height(96.dp),
                )
                Spacer(Modifier.height(14.dp))
                Text(
                    text = "Your adaptive AI workspace",
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    fontSize = 15.sp,
                    textAlign = TextAlign.Center,
                )
                Spacer(Modifier.height(40.dp))
                GradientButton(text = "Sign in", onClick = onSignIn)
                error?.let {
                    Spacer(Modifier.height(18.dp))
                    Text(
                        text = it,
                        color = MaterialTheme.colorScheme.error,
                        fontSize = 13.sp,
                        textAlign = TextAlign.Center,
                    )
                }
                Spacer(Modifier.height(28.dp))
                Text(
                    text = "Secured by Keycloak",
                    color = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.7f),
                    fontSize = 12.sp,
                )
            }
        }
    }
}

/** The signature indigo→purple pill button used for the primary sign-in action. */
@Composable
private fun GradientButton(
    text: String,
    onClick: () -> Unit,
) {
    Box(
        modifier =
            Modifier
                .widthIn(min = 220.dp)
                .clip(RoundedCornerShape(26.dp))
                .background(AstralColors.AccentBrush)
                .clickable(onClick = onClick)
                .padding(vertical = 15.dp, horizontal = 32.dp),
        contentAlignment = Alignment.Center,
    ) {
        Text(text = text, color = Color.White, fontSize = 16.sp, fontWeight = FontWeight.SemiBold)
    }
}
