import { TEXTURE_FORMAT, type DecodedPlane } from "./spx1.ts";

export type RenderMode = "single" | "difference" | "difference-plane" | "rgb";
export type RenderPreset = "linear" | "asinh" | "log";

export interface RendererWindow {
  lo: number;
  hi: number;
  s: number;
}

const PRESET_INDEX: Record<RenderPreset, number> = {
  linear: 0,
  asinh: 1,
  log: 2,
};

const VERTEX_SHADER = `#version 300 es
in vec2 aPos;
out vec2 vUv;
void main() {
  vUv = aPos * 0.5 + 0.5;
  gl_Position = vec4(aPos, 0.0, 1.0);
}`;

const FRAGMENT_SHADER = `#version 300 es
precision highp float;
in vec2 vUv;
out vec4 outColor;
uniform sampler2D uA;
uniform sampler2D uB;
uniform sampler2D uC;
uniform int uMode;
uniform int uPreset;
uniform float uLo;
uniform float uHi;
uniform float uSigma;
uniform float uDiffSigma;

float windowT(float value) {
  float span = max(uHi - uLo, 1e-6);
  float normalized = clamp((value - uLo) / span, 0.0, 1.0);
  if (uPreset == 1) {
    float sigma = max(uSigma, 1e-6);
    float denominator = max(asinh(span / sigma), 1e-6);
    return clamp(asinh(max(value - uLo, 0.0) / sigma) / denominator, 0.0, 1.0);
  }
  if (uPreset == 2) {
    return clamp(log(1.0 + 9.0 * normalized) / log(10.0), 0.0, 1.0);
  }
  return normalized;
}

vec3 colorRamp(float t) {
  vec3 low = vec3(0.02, 0.03, 0.12);
  vec3 mid = vec3(0.20, 0.55, 0.78);
  vec3 high = vec3(1.0, 0.93, 0.72);
  if (t < 0.5) {
    return mix(low, mid, t * 2.0);
  }
  return mix(mid, high, (t - 0.5) * 2.0);
}

vec3 differenceColor(float d) {
  vec3 negative = vec3(0.96, 0.55, 0.18);
  vec3 zero = vec3(0.02, 0.02, 0.03);
  vec3 positive = vec3(0.20, 0.65, 1.0);
  return d < 0.0 ? mix(zero, negative, -d) : mix(zero, positive, d);
}

void main() {
  vec2 uv = vec2(vUv.x, 1.0 - vUv.y);
  float a = texture(uA, uv).r;
  if (uMode == 1) {
    float b = texture(uB, uv).r;
    if (isnan(a) || isnan(b) || isinf(a) || isinf(b)) {
      outColor = vec4(0.45, 0.05, 0.55, 1.0);
      return;
    }
    float span = max(uHi - uLo, 1e-6);
    outColor = vec4(differenceColor(clamp((b - a) / span, -1.0, 1.0)), 1.0);
    return;
  }
  if (uMode == 2) {
    if (isnan(a) || isinf(a)) {
      outColor = vec4(0.45, 0.05, 0.55, 1.0);
      return;
    }
    float scale = max(
      (uDiffSigma > 0.0 ? uDiffSigma : uSigma) * 3.0,
      1e-6
    );
    outColor = vec4(differenceColor(clamp(a / scale, -1.0, 1.0)), 1.0);
    return;
  }
  if (uMode == 3) {
    float b = texture(uB, uv).r;
    float c = texture(uC, uv).r;
    if (isnan(a) || isnan(b) || isnan(c) || isinf(a) || isinf(b) || isinf(c)) {
      outColor = vec4(0.45, 0.05, 0.55, 1.0);
      return;
    }
    outColor = vec4(windowT(a), windowT(b), windowT(c), 1.0);
    return;
  }
  if (isnan(a) || isinf(a)) {
    outColor = vec4(0.45, 0.05, 0.55, 1.0);
    return;
  }
  outColor = vec4(colorRamp(windowT(a)), 1.0);
}`;

export class RendererError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "RendererError";
  }
}

function compileShader(
  gl: WebGL2RenderingContext,
  type: number,
  source: string,
): WebGLShader {
  const shader = gl.createShader(type);
  if (!shader) throw new RendererError("WebGL2 could not create a shader");
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    const log = gl.getShaderInfoLog(shader) ?? "unknown shader error";
    gl.deleteShader(shader);
    throw new RendererError(`shader compile failed: ${log}`);
  }
  return shader;
}

/**
 * Two-texture WebGL2 plane renderer. Every upload is `R16F`.
 */
export class PlaneRenderer {
  readonly canvas: HTMLCanvasElement;
  private gl: WebGL2RenderingContext;
  private program: WebGLProgram;
  private textures: [WebGLTexture, WebGLTexture, WebGLTexture];
  private window: RendererWindow = { lo: 0, hi: 1, s: 0.1 };
  private diffSigma = 0;
  private mode: RenderMode = "single";
  private preset: RenderPreset = "linear";
  private uniforms: Record<string, WebGLUniformLocation | null>;

  constructor(canvas: HTMLCanvasElement) {
    this.canvas = canvas;
    const gl = canvas.getContext("webgl2", {
      antialias: false,
      depth: false,
      preserveDrawingBuffer: true,
    });
    if (!gl) {
      throw new RendererError(
        "WebGL2 is required to render SPX1 planes; this browser did not provide it.",
      );
    }
    this.gl = gl;
    const vertex = compileShader(gl, gl.VERTEX_SHADER, VERTEX_SHADER);
    const fragment = compileShader(gl, gl.FRAGMENT_SHADER, FRAGMENT_SHADER);
    const program = gl.createProgram();
    if (!program) throw new RendererError("WebGL2 could not create a program");
    gl.attachShader(program, vertex);
    gl.attachShader(program, fragment);
    gl.linkProgram(program);
    gl.deleteShader(vertex);
    gl.deleteShader(fragment);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      const log = gl.getProgramInfoLog(program) ?? "unknown link error";
      gl.deleteProgram(program);
      throw new RendererError(`shader link failed: ${log}`);
    }
    this.program = program;
    gl.useProgram(program);

    const buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(
      gl.ARRAY_BUFFER,
      new Float32Array([-1, -1, 3, -1, -1, 3]),
      gl.STATIC_DRAW,
    );
    const position = gl.getAttribLocation(program, "aPos");
    gl.enableVertexAttribArray(position);
    gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);

    this.textures = [
      this.makeTexture(),
      this.makeTexture(),
      this.makeTexture(),
    ];
    this.uniforms = {
      uA: gl.getUniformLocation(program, "uA"),
      uB: gl.getUniformLocation(program, "uB"),
      uC: gl.getUniformLocation(program, "uC"),
      uMode: gl.getUniformLocation(program, "uMode"),
      uPreset: gl.getUniformLocation(program, "uPreset"),
      uLo: gl.getUniformLocation(program, "uLo"),
      uHi: gl.getUniformLocation(program, "uHi"),
      uSigma: gl.getUniformLocation(program, "uSigma"),
      uDiffSigma: gl.getUniformLocation(program, "uDiffSigma"),
    };
    gl.uniform1i(this.uniforms.uA, 0);
    gl.uniform1i(this.uniforms.uB, 1);
    gl.uniform1i(this.uniforms.uC, 2);
    canvas.dataset.textureFormat = TEXTURE_FORMAT.internalFormat;
  }

  private makeTexture(): WebGLTexture {
    const gl = this.gl;
    const texture = gl.createTexture();
    if (!texture) throw new RendererError("WebGL2 could not create a texture");
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    return texture;
  }

  setWindow(window: RendererWindow): void {
    this.window = { lo: window.lo, hi: window.hi, s: window.s };
  }

  setDifferenceSigma(sigma: number): void {
    this.diffSigma = Number.isFinite(sigma) && sigma > 0 ? sigma : 0;
  }

  setMode(mode: RenderMode): void {
    this.mode = mode;
  }

  setPreset(preset: RenderPreset): void {
    this.preset = preset;
  }

  setPlane(plane: DecodedPlane, slot: 0 | 1 | 2): void {
    if (plane.header.dtype !== 1) {
      throw new RendererError("only float16 SPX1 planes may be uploaded");
    }
    const gl = this.gl;
    gl.activeTexture(
      slot === 0 ? gl.TEXTURE0 : slot === 1 ? gl.TEXTURE1 : gl.TEXTURE2,
    );
    gl.bindTexture(gl.TEXTURE_2D, this.textures[slot]);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    gl.texImage2D(
      gl.TEXTURE_2D,
      0,
      gl.R16F,
      plane.width,
      plane.height,
      0,
      gl.RED,
      gl.HALF_FLOAT,
      plane.data,
    );
  }

  draw(): void {
    const gl = this.gl;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const width = Math.max(1, Math.round(this.canvas.clientWidth * dpr));
    const height = Math.max(1, Math.round(this.canvas.clientHeight * dpr));
    if (this.canvas.width !== width || this.canvas.height !== height) {
      this.canvas.width = width;
      this.canvas.height = height;
    }
    gl.viewport(0, 0, width, height);
    gl.useProgram(this.program);
    const modeIndex: Record<RenderMode, number> = {
      single: 0,
      difference: 1,
      "difference-plane": 2,
      rgb: 3,
    };
    gl.uniform1i(this.uniforms.uMode, modeIndex[this.mode]);
    gl.uniform1i(this.uniforms.uPreset, PRESET_INDEX[this.preset]);
    gl.uniform1f(this.uniforms.uLo, this.window.lo);
    gl.uniform1f(this.uniforms.uHi, this.window.hi);
    gl.uniform1f(this.uniforms.uSigma, this.window.s);
    gl.uniform1f(this.uniforms.uDiffSigma, this.diffSigma);
    gl.clearColor(0.02, 0.02, 0.03, 1);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }

  dispose(): void {
    const gl = this.gl;
    for (const texture of this.textures) gl.deleteTexture(texture);
    gl.deleteProgram(this.program);
  }
}
