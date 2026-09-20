@Library('nabster-ci') _

pipeline {
    agent {
        docker {
            image 'python:3.11'
        }
    }

    stages {
        stage('Notify start') {
            steps {
                notifyTelegram('started')
            }
        }

        stage('Install') {
            steps {
                sh 'python -m venv .venv'
                sh '.venv/bin/python -m pip install -r requirements.txt'
            }
        }

        stage('Unit tests') {
            steps {
                sh '.venv/bin/python -m tests'
            }
        }
    }

    post {
        success {
            notifyTelegram('success')
        }

        failure {
            notifyTelegram('failed')
        }
    }
}
