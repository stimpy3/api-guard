// Jenkins Shared Library step.
//
// This is the answer to "a company with 100 microservices does not write 100
// pipelines". Configure this repo as a Global Pipeline Library in Jenkins, and
// a consuming Jenkinsfile collapses to:
//
//     @Library('api-guard') _
//     apiGuard(generatedSpec: 'generated.yaml', url: 'http://api:8000')
//
// One central copy of the integration logic, versioned like anything else.

def call(Map args = [:]) {
  def config        = args.get('config', 'api-guard.yaml')
  def version       = args.get('version', '1')
  def image         = args.get('image', "sohanbhadalkar/api-guard:${version}")
  def generatedSpec = args.get('generatedSpec', null)
  def url           = args.get('url', null)
  def only          = args.get('only', null)
  def network       = args.get('network', null)
  def failBuild     = args.get('failBuild', true)

  def flags = ["check", "--config", config]
  if (generatedSpec) { flags += ["--generated-spec", generatedSpec] }
  if (url)           { flags += ["--url", url] }
  if (only)          { flags += ["--only", only] }

  def netArg = network ? "--network ${network}" : "--add-host=host.docker.internal:host-gateway"

  sh "docker pull ${image}"

  def code = sh(
    returnStatus: true,
    script: "docker run --rm ${netArg} -v \$(pwd):/work -w /work ${image} ${flags.join(' ')}"
  )

  // Always publish, including on failure — the report is most useful exactly
  // when the build went red.
  junit allowEmptyResults: true, testResults: 'api-guard-report/junit.xml'
  archiveArtifacts artifacts: 'api-guard-report/**', allowEmptyArchive: true

  if (fileExists('api-guard-report/report.md')) {
    echo readFile('api-guard-report/report.md')
  }

  // 1 and 2 mean different things and should not be collapsed. A config error
  // reported as "breaking change detected" sends somebody hunting for a change
  // that does not exist.
  if (code == 1) {
    if (failBuild) { error("api-guard: the contract would break consumers.") }
    unstable("api-guard: the contract would break consumers.")
  } else if (code == 2) {
    error("api-guard could not reach a conclusion (config or tooling problem, not a contract violation).")
  }

  return code
}
